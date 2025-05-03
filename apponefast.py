import os
import json
import httpx
import base64
import datetime
import openai
import polars as pl
import boto3
import re
from io import BytesIO
from typing import List, TypedDict
from e2b_code_interpreter import Sandbox
from langgraph.graph import StateGraph
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn
from fastapi import FastAPI
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
import traceback
from dotenv import load_dotenv
from openai import AsyncOpenAI
load_dotenv()

S3_BUCKET_NAME = "code-interpreter-s3"

apponefast = FastAPI()
apponefast.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@apponefast.get("/")
async def serve_html():
    return FileResponse("chatinterface.html")

from typing import Dict

# ✅ Store active WebSocket connections per chat_id
connected_chats: Dict[str, WebSocket] = {}

# ✅ Function to send messages to a specific user
async def stream_to_frontend(chat_id: str, event: str, message):
    """Send messages to a specific user's frontend via WebSocket."""
    preview = str(message)[:30]
    print(f"📡 Sending to {chat_id}: {event} => {preview}...")
    
    if chat_id is None:
        print("⚠️ chat_id is None — probably missing in state.")
        return
    
    websocket = connected_chats.get(chat_id)
    if websocket:
        try:
            await websocket.send_text(json.dumps({"event": event, "message": str(message)}))
        except Exception as e:
            print(f"❌ WebSocket Error for {chat_id}: {e}")
            connected_chats.pop(chat_id, None)
    else:
        print(f"⚠️ No WebSocket connection found for chat_id: {chat_id}")


# 🟢 Step 1: Define State Schema
class CodeInterpreterState(TypedDict):
    user_query: str
    csv_file_paths: list[str]
    csv_info_list: list[dict]
    generated_code: str
    execution_result: str
    error: str
    sandbox: Sandbox
    current_step_index: int  # Current step being processed
    current_step_code: str  # Current step's code
    stepwise_code: list[dict]  # All steps, each with description, code, status
    uploaded_files: dict[str, str]  # ✅ Tracks uploaded files (file_path -> S3 URL)
    final_response: str
    context: list[str]
    user_id: str
    chat_id: str
    steps: list[str]
    sandbox_id: str


# 🟢 Step 2: Initialize Langgraph with State
graph = StateGraph(CodeInterpreterState)

# ✅ WebSocket Route
global_websocket = None  # Stores the WebSocket connection

# ✅ WebSocket Route with chat_id support
@apponefast.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connection for a specific user."""
    await websocket.accept()
    
    chat_id = websocket.query_params.get("chat_id")
    if not chat_id:
        await websocket.close(code=4001)
        print("❌ No chat_id provided. Connection closed.")
        return

    connected_chats[chat_id] = websocket
    print(f"✅ WebSocket connected: {chat_id}")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("event") == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))

            elif message.get("event") == "bot_message":
                # You can handle incoming bot_message logic if needed
                print(f"📩 Received message from {chat_id}: {message['message']}")

    except WebSocketDisconnect:
        print(f"❌ WebSocket Disconnected: {chat_id}")
        connected_chats.pop(chat_id, None)


# 🟢 Step 3: Extract CSV Info and Upload to E2B
import polars as pl
import os
import polars as pl
import httpx
from io import BytesIO

def is_plain_text(text):
    """
    Checks if the given text is simple text (not JSON, lists, dictionaries, or structured data).
    """

    # Attempt JSON parsing
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (dict, list)):  # If it's valid JSON and is dict/list, reject
            print("Failed in json loads")
            return False
    except (ValueError, TypeError):
        pass

    # Check for explicit dictionary or list indicators at the start and end (likely structured)
    if text.strip().startswith(("{", "[")) and text.strip().endswith(("}", "]")):
        print("Failed in { [ ] }")
        return False

    # Check for tabular-like structure
    if re.search(r"[-\d.]+\s+[-\d.]+", text):  # Detects matrix-like numbers
        print("Failed in first")
        return False  
    
    # 🔹 **FIXED:** Ignore dictionary-like printed strings, but catch deeply structured JSON
    if re.search(r"^\{\s*['\"]?\w+['\"]?\s*:\s*['\"]?\w+['\"]?\s*\}$", text.strip()):
        print("Failed in second")
        return False

    print("returned true")
    return True

def read_csv_from_url(url):
    print("read_csv_from_url")
    with httpx.stream("GET", url) as response:
        if response.status_code != 200:
            raise Exception(f"Failed to fetch {url}, status: {response.status_code}")

        # Stream the content into memory (BytesIO for binary data)
        buffer = BytesIO()
        for chunk in response.iter_bytes():
            buffer.write(chunk)

        buffer.seek(0)  # Rewind for Polars to read

        # Polars can read directly from BytesIO (memory file)
        df = pl.read_csv(buffer)

    return df

import os
import httpx
from e2b_code_interpreter import Sandbox

from io import BytesIO

def download_file_to_sandbox(sbx: Sandbox, url: str, sandbox_folder: str = "home/atharv"):
    filename = os.path.basename(url)
    sandbox_path = f"{sandbox_folder}/{filename}"
    full_path = f"/home/user/{sandbox_path}"

    timeout = httpx.Timeout(30.0, read=30.0, connect=10.0)
    with httpx.stream("GET", url, timeout=timeout) as response:
        if response.status_code != 200:
            raise Exception(f"Failed to download {url}, status: {response.status_code}")
        buffer = BytesIO()
        for chunk in response.iter_bytes():
            buffer.write(chunk)
        buffer.seek(0)
        sbx.files.write(sandbox_path, buffer.read())  # write bytes to sandbox

    return full_path



def upload_to_s3_direct(content: bytes, file_name: str, bucket_name: str, s3_folder="results"):
    s3_client = boto3.client(
        's3',
        region_name='us-east-2',
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )

    s3_key = f"{s3_folder}/{file_name}"

    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=content
        )

        s3_url = f"https://{bucket_name}.s3.us-east-2.amazonaws.com/{s3_key}"
        print(f"✅ Uploaded directly to S3: {s3_url}")
        return s3_url
    except Exception as e:
        print(f"❌ Failed to upload in direct {file_name} to S3: {e}")
        return None


async def generate_csv_description(csv_info, state: CodeInterpreterState):
    """
    Uses OpenAI API to generate a short description of the dataset based on column names, data types, and sample data.
    """
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    prompt = f"""
    You are a data scientist. Based on the following dataset details, generate a short description (30-50 words) explaining what the dataset likely represents.

    - **File Name**: {csv_info['filename']}
    - **Columns**: {', '.join(csv_info['column_names'])}
    - **Data Types**: {', '.join(csv_info['data_types'])}
    - **Sample Data**:
    {csv_info['sample_data']}
    
    Provide a concise but informative description.
    """

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a data analysis expert. Give a 50-60 words desciption of the csv based on the details and give a story."},
                  {"role": "user", "content": prompt}],
        temperature=0.2,
        stream=True
    )
    chat_id = state.get("chat_id")
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            # print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(chat_id, "bot_message", partial_text)

    await stream_to_frontend(chat_id, "bot_message", "\n \n")
    return collected_text

async def extract_csv_info(state: CodeInterpreterState) -> CodeInterpreterState:

    if (not state.get("csv_file_paths")) and (state.get("csv_info_list")):
        print("✅ No new CSV files and existing CSV info found. Skipping extraction.")
        return state

    print("🚀 New CSVs detected or no existing info. Processing CSVs now...")


    # state["sandbox"] = Sandbox.connect(state.get("sandbox_id"))
    # print(f"This is ur sandox {state['sandbox']}")

    max_retries = 5  # Set a maximum retry limit to avoid infinite loop
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            # Start retry loop
            sbx = Sandbox.connect(state.get("sandbox_id"))
            print(f"This is ur sandox {state['sandbox']}")
            sbx.commands.run("pip install polars")
            sbx.commands.run("pip install pyarrow")
            sbx.commands.run("pip install mpld3")
            
            # state["sandbox"] = sbx

            csv_info_list = []
            sandbox_paths = []

            csv_info_list = state.get("csv_info_list", [])
            existing_s3_urls = {info.get("s3_url") for info in csv_info_list if "s3_url" in info}


            for csv_path in state["csv_file_paths"]:
                print(f"Processing file: {csv_path}")
                if csv_path in existing_s3_urls:
                    print(f"⚠️ Skipping already processed file: {csv_path}")
                    continue
        
                # Read with Polars
                if csv_path.startswith("http"):
                    df = read_csv_from_url(csv_path)
                else:
                    df = pl.read_csv(csv_path)

                print(f"Created Dataframes for file: {csv_path}")
                column_names = df.columns
                num_rows = df.height  # 'height' is the row count in Polars
                data_types = {col: str(dtype) for col, dtype in zip(df.columns, df.dtypes)}
                sample_data = df.head(3).to_pandas().to_string(index=False)

                print("Data gathering done")

                # Upload each CSV file to sandbox 
                if csv_path.startswith("http"):
                    sandbox_path = download_file_to_sandbox(sbx, csv_path)
                    sandbox_paths.append(sandbox_path)
                else:
                    with open(csv_path, "rb") as f:
                        sandbox_path = sbx.files.write(f"home/atharv/{os.path.basename(csv_path)}", f)
                    sandbox_paths.append(f"/home/user/home/atharv/{os.path.basename(csv_path)}")

                print("Sandbox paths: ", sandbox_paths[-1])
                csv_info_list.append({
                    "filename": os.path.basename(csv_path),
                    "sandbox_path": sandbox_paths[-1],
                    "column_names": column_names,
                    "num_rows": num_rows,
                    "data_types": data_types,
                    "sample_data": sample_data,
                    "s3_url": csv_path  # ✅ Add original path
                })

                print(f"Finished Processing file: {csv_path}")

            state["csv_info_list"] = csv_info_list
            state["execution_result"] = "✅ All CSV files uploaded and info extracted"
            state["error"] = None

            # Generate descriptions for each CSV
            for csv_info in csv_info_list:
                collected_text = await generate_csv_description(csv_info, state)
                state["final_response"] += collected_text

            # No exception, exit the loop
            break

        except Exception as e:
            print(f"❌ Error processing CSV files: {str(e)}. Retrying... ({retry_count + 1}/{max_retries})")
            state["execution_result"] = f"❌ Error processing CSV files: {str(e)}"
            state["error"] = str(e)
            retry_count += 1
            if retry_count >= max_retries:
                print("❌ Maximum retries reached. Exiting.")
                state["execution_result"] = "❌ Maximum retries reached. Task failed."
                break
    
    print(f"This is ur object {state['csv_info_list']}")

    return state


async def generate_steps(state: CodeInterpreterState) -> CodeInterpreterState:
    print("hello gs")
    state["steps"].append(state['user_query'])
    # stream_to_frontend("bot_message","The following CSV files is/are available:\n")
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    chat_id = state.get("chat_id")
    csv_schema_text = ""
    for i, csv_info in enumerate(state["csv_info_list"], start=1):
        csv_schema_text += f"""
        CSV {i}:
        - File: {csv_info['filename']}
        - Columns: {csv_info['column_names']}
        """

    classification_prompt = f"""
        A user has submitted the following query:
        "{state['user_query']}"

        They uploaded the following CSV files:
        {csv_schema_text}

        Please classify the query into one of the following:
        - simple_fact
        - moderate_analysis
        - complex_analysis
        - unrelated

        Only return one of the four labels above.
    """
    classification_response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "system", "content": "You are a query classifier for a data assistant."
        }, {
            "role": "user", "content": classification_prompt
        }]
    )
    query_type = classification_response.choices[0].message.content.strip().lower()
    print(f"🔍 Query Type: {query_type}")

    if query_type not in ["simple_fact", "moderate_analysis", "complex_analysis", "unrelated"]:
        query_type = "moderate_analysis"  # fallback

    if query_type == "unrelated":
        # Generate a natural response via OpenAI
        chat_prompt = f"""
        A user has uploaded a CSV file but asked the following question instead:

        "{state['user_query']}"
        Previous Context : {state['steps']} 
        You can answer directly from this if required, basically it is previously asked context. 

        This question is unrelated to data analysis. Please respond in a kind, conversational tone as a friendly assistant. Keep it short and warm, like you're chatting with a curious user. Don't reference CSVs unless they bring it up again.
        """

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a warm and witty assistant that chats casually with users."},
                {"role": "user", "content": chat_prompt}
            ],
            temperature=0.8,
            stream=True
        )

        collected_text = ""
        async for chunk in response:
            if chunk.choices[0].delta.content:
                partial_text = chunk.choices[0].delta.content
                collected_text += partial_text
                await stream_to_frontend(chat_id, "bot_message", partial_text)

        state["final_response"] += collected_text
        state["stepwise_code"] = []  # No code steps to run
        state["current_step_code"] = ""
        state["current_step_index"] = 0
        return state

    num_steps_instruction = {
        "simple_fact": "generate **1 step** (loading dataset and resolving query).",
        "moderate_analysis": "generate **up to 2 steps**.",
        "complex_analysis": "generate **3-5 steps**, only if needed.",
    }[query_type]


    csv_info_text = "The following CSV files is/are available:\n"   
    for csv_info in state["csv_info_list"]:
        csv_info_text += f"""
        - **File:** {csv_info['filename']}
        - **Path:** {csv_info['sandbox_path']}
        - **Columns:** {csv_info['column_names']}
        - **Rows:** {csv_info['num_rows']}
        - **Sample Data:**
        {csv_info['sample_data']}
        """

    steps_prompt = f"""
        You are a **data analyst assistant**. Based on the user's query and the available CSV files, generate a **stepwise process** for resolving the user query. 

        **Tone & Style:**
        - Be natural and engaging, keeping steps concise and to the point.
        - Keep responses **natural, engaging, and interactive**, like a human assistant who is actively trying to help, keep steps in first person.
        - Avoid redundancy. If the query can be answered in **one step**, do so efficiently.
        - Ensure that the steps will be executed in **Python** and will work in the same sandbox environment to maintain state across steps.
        - Vary phrasing to avoid repetition. Instead of starting every step with "I will do this," make it dynamic—sometimes say "Let me check that," "Next, I'll handle...," or "To make sure we get the best results, I'll also..."
        - For **simple fact queries**, generate **only 1 step**, combining dataset loading and query resolution in one go.
            - For example, if the query asks for row count, combine loading and counting rows in one step.
        - For **moderate analysis**, generate **1 step**, combining data loading, processing, and analysis in one step.
        - For **complex analysis**, generate **2-5 steps**, only if necessary. The task should be broken down into **distinct, actionable steps** with no redundant operations.
            - Ensure each step is unique and doesn't repeat work already done (like reloading data).
        - Avoid unnecessary steps like saving files unless explicitly requested.
        - If the task is simple (like counting rows), only one step is needed.
        - Combine dataset loading and simple analysis into one step if possible.
        - Ensure each step contributes to the resolution of the query and that the steps are actionable in Python.
        - Each new step **must build upon previous steps**, using the same sandbox environment.
        - If multiple steps are necessary, ensure they are logically distinct and non-repetitive.


        **Previous context (summaries of earlier tasks, use this in response if required): if asked something similar, u can refer and give that again saying u have already asked this but will redo it.**
        {state['steps']}

        **User Query:**
        "{state['user_query']}"

        **Available CSV Files:**
        {csv_info_text}

        **Guidelines for Step Generation:**
        - For **simple fact queries**, combine dataset loading and the task into **one step**.
        - For **moderate analysis**, generate **1 step** to cover dataset loading, processing, and analysis.
        - For **complex analysis**, generate **2–5 steps** only if necessary. 
        - Each step must be **logically distinct** and should avoid repeating operations.
        - **Avoid adding steps just to display or summarize outputs**, like “finally show the plots” — assume outputs are displayed inline with analysis steps.
        - Avoid steps like “generate visualizations” AND then another step “display visualizations.” These should be combined.
        - Only include a new step if it introduces new logic, computation, or visualization — not just to summarize or repeat.

        **Return Format:**
        - Provide only the **numbered list of steps**, keeping them **concise yet clear**.
        - **Do not generate any code**, only structured steps..
    """


    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a data analysis expert."},
                  {"role": "user", "content": steps_prompt}],
        temperature=0.2,
        stream=True
    )
    print("\n🔹 OpenAI Generated Steps:")
    # await stream_to_frontend("bot_message", "\n🔹 OpenAI Generated Steps:")
    
    print(chat_id," This is the chat_id")
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            # print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(chat_id, "bot_message", partial_text)

    # steps_text = response.choices[0].message.content.strip()
    steps_text = collected_text
    state["steps"].append(collected_text)
    state["final_response"] += collected_text
    steps = [line.strip() for line in steps_text.split("\n") if line.strip() and re.match(r"^\d+\.", line)]
    # print("\n🔹 OpenAI Generated Steps:")
    # for step in steps:
    #     print(f"- {step}")

    state["stepwise_code"] = [
        {"step": idx, "description": step, "code": "", "status": "pending"}
        for idx, step in enumerate(steps, start=1)
    ]

    state["current_step_index"] = 0
    state["current_step_code"] = ""

    #print(f"✅ Fetched {len(state['stepwise_code'])} steps.")
    return state


# 🟢 Step 4: Generate Python Code Using LLM
async def generate_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    #print(state["current_step_index"], " This is our step index curently getting executed")
    print("generate_python_code")
    chat_id = state.get("chat_id")
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    if not state["stepwise_code"]:
        print("No steps to process.")
        return state

    current_step = state["stepwise_code"][state["current_step_index"]]
    step_description = current_step["description"]

    print(f"\n🔵 Fetching Python code for Step {current_step['step']}: {step_description}")
    await stream_to_frontend(chat_id, "bot_message", f"\n\n🔵 Fetching Python code ... \n\n Step : {step_description}\n\n")

    state["final_response"] += f"\n\n🔵 Fetching Python code ... \n\n Step : {step_description}\n\n"

    csv_info_text = "The following CSV files is/are available:\n"
    for csv_info in state["csv_info_list"]:
        csv_info_text += f"""
        - **File:** {csv_info['filename']}
        - **Path:** {csv_info['sandbox_path']}
        - **Columns:** {csv_info['column_names']}
        - **Rows:** {csv_info['num_rows']}
        - **Sample Data :**
        {csv_info['sample_data']}
        """

    step_prompt = f"""
        {csv_info_text}

        User Query(Just keep this in hindsight, Just give the code for the current step!):
        {state['user_query']}

        You are currently working on **Step {state["current_step_index"]}: {step_description}**

        Please generate **ONLY Python code** for this step. Do not write any explanations or comments. Return only the code in a code block.

        The available CSV files/file have the following details & PATHS TO ACCCES THEM ARE ALSO GIVEN PLS USE THEM FURTHUR:
        {csv_info_text}
        - Use the same CSV PATH above compulsarily. Do not use any other path.
        Guidelines:
        - If the user query **requires only a factual response** (e.g., "How many rows?", "What are the column names?"), return a direct answer **inside a print statement**, e.g.:print("<some relevant text>:", <response>)
        - Use polars instead of pandas if the csv has more than 50000 rows, otherwise use pandas only for creation of dataframe. 
        - if asked for analysis, give some plots as well.
        - ** If using datframes we usually get this error, keep this in mind, AttributeError: 'DataFrame' object has no attribute 'groupby' and dont use groupby with dataframes when using polars**
        - Use the correct sandbox file paths when reading the CSV files.
        - Use the same DataFrames across steps (assume they are already defined from previous steps).
        - Do not save matplotlib plots (just show them using plt.show()). Only save plotly visualizations using fig.write_html.
        - Always set the title of the plot using plt.title(), also same name should be given to the json file as title STRICTLY.
        ### **For Matplotlib/Seaborn Plots:**
            - **Show plots using `plt.show()`**.
            - **For Bar Plots**:
                - Identify the **highest bar** (peak value).
                - Identify the **lowest bar** (minimum value).
                - Calculate the **mean of all values**.
                - Identify **significant differences between categories**.
                - Save this extracted data in JSON format to `/home/user/home/atharv/<same as what u gave in plt.title()>.json`
            - **For Scatter Plots**:
                - Identify **min/max x values**.
                - Identify **min/max y values**.
                - Detect **clusters or outliers**.
                - Identify **positive/negative/no correlation** between variables.
                - Save this extracted data in JSON format to `/home/user/home/atharv/<same as what u gave in plt.title()>.json`
            - **For Line Plots**:
                - Identify **min/max points**.
                - Identify **overall trend (increasing, decreasing, fluctuating)**.
                - Find **points of steepest change**.
                - Save this extracted data in JSON format to `/home/user/home/atharv/<same as what u gave in plt.title()>.json`
            - **For Histograms**:
                - Identify **most frequent range of values**.
                - Identify **outliers or skewness**.
                - Save this extracted data in JSON format to `/home/user/home/atharv/<same as what u gave in plt.title()>.json`
            - **For Box Plots**:
                - Identify **median, IQR, min, max, outliers**.
                - Save this extracted data in JSON format to `/home/user/home/atharv/<same as what u gave in plt.title()>.json`
            - **For Pie Charts**:
                - Identify **largest and smallest categories**.
                - Calculate **percentage contribution of each category**. 
                - Save this extracted data in JSON format to `/home/user/home/atharv/<same as what u gave in plt.title()>.json`
            - **Generate above stats for each plot at the end ad NOT IN A SINGLE JSON FILE BUT IN INDIVIDUAL JSON FILE per plot** 
            - STRICTLY DONT use THIS plt.savefig()
            - Do not plot the same plots using plotly and matplotlib, either plot it using matplotlib or plotly.
        - Do not forget to create JSON PER PLOT(DO NOT AGGREGATE INTO ONE FILE) if you are genearting any kind of plots, STRICTLY you should create JSON for a plot(if at all we have plots).
        - Always set the title of the plot using `plt.title("<TITLE>")`.
        - Then also set `fig.suptitle("<TITLE>")` — E2B reads this for chart metadata like `res.chart.title`.
        - Create plots using `fig = plt.figure(...)` and assign them explicitly (e.g., `fig1`, `fig2`, ...).
        - Displaying the plots using `plt.show()`
        - This ensures all plots are captured by the sandbox and included in the results.
        - Any intermediate CSV files (like cleaned data) must be saved to /home/user/home/atharv/cleaned_step{state["current_step_index"]}.csv or similar.
        - If generating a data which is tabular, dont print it. Create a csv and save it.
        - ** Some Common errors **
            - Object of type int64 is not JSON serializable
        - While writing python code keep this errors in mind, so that you dont write the code which generates above errors.
    """
    print(step_prompt)
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a Python expert."},
                  {"role": "user", "content": step_prompt}],
        temperature=0.2,
        stream=True
    )
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(chat_id, "bot_message", partial_text)

    # step_code = response.choices[0].message.content
    step_code = collected_text 
    state["final_response"] += collected_text
    code_match = re.search(r"```python\n(.*?)```", step_code, re.DOTALL)
    step_code_cleaned = code_match.group(1).strip() if code_match else step_code.strip()

    #print(step_code_cleaned)

    state["stepwise_code"][state["current_step_index"]]["code"] = step_code_cleaned
    state["current_step_code"] = step_code_cleaned

    #print(f"✅ Code fetched for Step {current_step['step']}")
    return state

import boto3
import json
import re
from urllib.parse import urlparse

# Initialize S3 client
s3_client = boto3.client('s3', region_name='us-east-2', aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"))  # Optional if using env variables)

async def get_json_and_generate_description(png_s3_url, bucket_name, state: CodeInterpreterState):
    """
    1. Extracts the chart title from the PNG S3 URL.
    2. Finds the corresponding JSON file in S3.
    3. Reads the JSON file and sends it to OpenAI for a description.
    """

    # Step 1: Extract PNG filename from the S3 URL
    parsed_url = urlparse(png_s3_url)
    filename = parsed_url.path.split("/")[-1]  # Get only the filename
    print(f"Extracted PNG filename: {filename}")

    # Step 2: Remove timestamp & step number to get the chart title
    match = re.match(r"step\d+_\d{8}_\d{6}_(.+)\.\w+$", filename)
    if not match:
        print("Filename format doesn't match expected pattern.")
        return None
    
    chart_title = match.group(1)  # Extracts "Survival Rate by Passenger Class"
    print(f"Extracted Chart Title: {chart_title}")

    # Step 3: Search for JSON file in S3 that matches chart title
    response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix="results/")
    if 'Contents' not in response:
        print("No JSON files found in S3.")
        return None

    json_file_key = None
    for obj in response['Contents']:
        file_key = obj['Key']
        if chart_title in file_key and file_key.endswith('.json'):
            json_file_key = file_key
            break

    if not json_file_key:
        print("No matching JSON file found in S3.")
        return None

    print(f"Found JSON file: {json_file_key}")

    # Step 4: Download and read JSON file
    json_obj = s3_client.get_object(Bucket=bucket_name, Key=json_file_key)
    json_content = json_obj['Body'].read().decode('utf-8')
    
    # Step 5: Generate description using OpenAI
    description = await generate_plot_description(json_content,chart_title,state)
    return description

async def generate_plot_description(json_content,chart_title,state: CodeInterpreterState):
    """
    Sends the JSON content to OpenAI ChatCompletion to generate a description.
    """
    chat_id = state.get("chat_id")
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a data analyst. Summarize the chart description in 30-40 words."},
            {"role": "user", "content": f"Here's the JSON describing a chart:\n{json_content}\nThis is a chart of {chart_title}. Summarize it in a very interactive way and like a data story"}
        ],
        temperature=0.7,
        max_tokens=100,
        stream=True
    )
    await stream_to_frontend(chat_id, "bot_message", '\n')
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(chat_id, "bot_message", partial_text)
    print(f"Generated Description: {collected_text}")
    return collected_text

import json
import numpy as np

def convert_to_serializable(obj):
    """
    Recursively converts non-serializable NumPy data types to standard Python types.
    """
    if isinstance(obj, np.integer):
        return int(obj)  # Convert int64 → int
    elif isinstance(obj, np.floating):
        return float(obj)  # Convert float64 → float
    elif isinstance(obj, dict):
        return {key: convert_to_serializable(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_to_serializable(value) for value in obj]
    return obj  

# 🟢 Step 5: Execute Python Code in E2B Sandbox
async def execute_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    """Uploads the generated script, executes it inside E2B Sandbox, and downloads result files if available."""
    #print("execute_python_code")
    try:
        chat_id = state.get("chat_id")
        state["error"] = None  # Reset any previous errors

        # ✅ Retrieve the Sandbox instance
        sbx = Sandbox.connect(state.get("sandbox_id"))
        if not sbx:
            raise ValueError("Sandbox instance not found in state.")

        #print(f"Executing in Sandbox with path: {state['csv_file_paths']}")
        #print(state["generated_code"],"This is the code going") 
        # result = sbx.run_code(state["generated_code"])
        step_index = state["current_step_index"]
        step_code = state["current_step_code"]
        print(f"🚀 Running Step {step_index + 1}")
        await stream_to_frontend(chat_id, "bot_message", f"\n🚀 Running Step {step_index + 1} in the Sandbox......\n")
        result = sbx.run_code(step_code)
        print("Result: ",result)        
        try:
         # ✅ Extract stdout correctly
            stdout_output = "\n".join(result.logs.stdout) if result.logs.stdout else "✅ Execution completed (no output)"
            # print(stdout_output)

            # Extract stderr correctly
            # Extract stderr output as a single string
            stderr_output = "\n".join(result.logs.stderr) if result.logs.stderr else ""

            if stderr_output and ("UserWarning" in stderr_output or "FutureWarning" in stderr_output):
                print("⚠️ Detected UserWarning (not a fatal error), continuing execution.")
                await stream_to_frontend(chat_id, "bot_message", "\n⚠️ Detected UserWarning (not a fatal error), continuing execution.")
                stderr_output = None  # Don't treat this as an error

            print(result.error,"<-result error")
            print(result.logs.stderr,"<-result.logs.stderr error")
            print(warning in result.logs.stderr for warning in ["UserWarning", "FutureWarning", "Warning"])
            print(any(warning in line for line in result.logs.stderr for warning in ["UserWarning", "FutureWarning", "Warning"]))

            # Only go inside IF:
            # - result.error is NOT None (actual error)
            # - stderr contains something OTHER THAN warnings (actual error messages)
            if result.error or (result.logs.stderr and not any(warning in line for line in result.logs.stderr for warning in ["UserWarning", "FutureWarning", "Warning"])):
                print("⚠️ Detected Error (fatal error), not continuing execution.")
                await stream_to_frontend(chat_id, "bot_message", "\n⚠️ Detected Error (fatal error), not continuing execution.")
                if result.error:
                    state["error"] = convert_to_serializable({
                        "name": result.error.name,        # Error type (e.g., TypeError, ValueError)
                        "message": result.error.value,    # Error message
                        "traceback": result.error.traceback.splitlines(),  # Full traceback as a list
                    })
                elif result.logs.stderr:
                    state["error"] = convert_to_serializable({
                        "stderr": result.logs.stderr,  # Convert stderr to JSON-safe format
                    })
                else:
                    state["error"] = None  # No error, reset state
                state["stepwise_code"][step_index]["status"] = "failed"
                return state  # 🚨 Exit early, don't proceed to file downloads!



            await stream_to_frontend(chat_id, "bot_message", f"\n🚀 Execution Completed in Sandbox\n\n")

            s3_url = None  # Initialize before using it
            for log in result.logs.stdout:
                print(log)
                if is_plain_text(log):
                    state["steps"].append(log)
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    file_name = f"step{step_index+1}_{timestamp}.txt"

                    # ✅ Upload to S3
                    s3_url = upload_to_s3_direct(log.encode(), file_name, S3_BUCKET_NAME)

                    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
                    await stream_to_frontend(chat_id, "bot_message", f"\n")
                    response = await client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "You are a text explainer. Summarize the text in 10-20 words."},
                            {"role": "user", "content": f"Here is the text :\n{log}\nSummarize it briefly. Also talk about the no.s/factual data if present in the text."}
                        ],
                        temperature=0.7,
                        max_tokens=50,
                        stream=True
                    )
                    await stream_to_frontend(chat_id, "bot_message", '\n')
                    collected_text = ""
                    async for chunk in response:
                        if chunk.choices[0].delta.content is not None:
                            partial_text = chunk.choices[0].delta.content
                            print(partial_text, end="", flush=True)  # Stream to terminal
                            collected_text += partial_text
                            await stream_to_frontend(chat_id, "bot_message", partial_text)
                    print(f"Generated Description: {collected_text}")
                    state["final_response"] += collected_text

                if s3_url:
                    await stream_to_frontend(chat_id, "bot_message", f'\n✅ Output saved: {s3_url}\n')
                    state["final_response"] += f'\n✅ Output saved: {s3_url}\n'

            #print("This is the result:", result)

                #print()
            state["stepwise_code"][step_index]["status"] = "success"
            state["error"] = None
            client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            prompt = f"""
                Please write a high-level, non-technical summary explaining the following code.
                Assume the reader is a business user, not a developer.
                Here is the code:
                {step_code}

                Please keep the explanation clear and simple in 20-30 words.
            """
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": "You are a code explainer."},
                        {"role": "user", "content": prompt}],
                temperature=0.2,
                stream=True
            )
            collected_text = ""
            async for chunk in response:
                if chunk.choices[0].delta.content is not None:
                    partial_text = chunk.choices[0].delta.content
                    print(partial_text, end="", flush=True)  # Stream to terminal
                    collected_text += partial_text
                    await stream_to_frontend(chat_id, "bot_message", partial_text)
            
            state["final_response"] += collected_text
                # print(response.choices[0].message.content)

        except Exception as e:
                print(e)

       

        # ✅ Extract ExecutionError if present
        error_message = None
        if result.error:  # If execution failed
            error_message = f"{result.error.name}: {result.error.value}\nTraceback:\n{result.error.traceback}"

        # ✅ Store outputs correctly
        # state["execution_result"] = stdout_output
        state["execution_result"] += f"\n\n🔹 Step {step_index + 1} Output:\n{stdout_output}"
        state["error"] = error_message if error_message else stderr_output

        list_files_script = """
        import os

        def list_all_files(directory):
            file_paths = []
            for root, dirs, files in os.walk(directory):
                for file in files:
                    file_paths.append(os.path.join(root, file))
            print("\\n".join(file_paths))  # Ensure paths are printed correctly

        list_all_files("/home/user/home/atharv/")
        """
        dir_result = sbx.run_code(list_files_script)

        # ✅ Extract and clean the list of file paths
        raw_output = dir_result.logs.stdout or []
        if raw_output:
            cleaned_file_paths = [path.strip() for path in raw_output[0].split("\n") if path.strip()]
        else:
            cleaned_file_paths = []


        print("📂 Files found in Sandbox:", cleaned_file_paths)
        # await stream_to_frontend(chat_id, "bot_message", f"\n📂 Files found in Sandbox: {cleaned_file_paths}")

        user_csv_path = cleaned_file_paths[0] if len(cleaned_file_paths) > 0 else ""
        #print(user_csv_path," user_csv_path")

        # ✅ Step 2: Download files
        # local_directory = "/Users/atharvwani/dloads"  # Change this to your preferred path
        # os.makedirs(local_directory, exist_ok=True)  # Ensure directory exists

        downloaded_files = []

        #print("This is result.results ",result.results)
        #print(len(result.results)," size")

        # Extract csv_info_list from state
        csv_info_list = state.get("csv_info_list")

        # Extract all CSV sandbox paths for efficient lookup
        csv_sandbox_paths = {csv_info["sandbox_path"] for csv_info in csv_info_list}

        for file_path in cleaned_file_paths:
            if not file_path.endswith(".json"):
                continue  # Skip non-JSON files

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            try:
                # Read JSON file as binary
                content = sbx.files.read(file_path)
                if file_path in csv_sandbox_paths:
                    continue    

                if isinstance(content, str):
                    content = content.encode()  # Convert string to bytes if necessary

                orig_file_name = os.path.basename(file_path)
                file_name = f"step{step_index+1}_{timestamp}_{orig_file_name}"
                
                # Direct upload to S3
                s3_url = upload_to_s3_direct(content, file_name, S3_BUCKET_NAME)

                if s3_url:
                    state["final_response"] += f'\n✅ Uploaded JSON to S3: {s3_url}'
                    # await stream_to_frontend(chat_id, "bot_message", f'\n✅ Uploaded JSON to S3: {s3_url}')
            
            except Exception as e:
                print(f"⚠️ Error uploading {file_path}: {e}")
                await stream_to_frontend(chat_id, "bot_message", f"\n⚠️ Error uploading {file_path}: {e}")

        # Second loop: Upload all other files (excluding JSON)
        for file_path in cleaned_file_paths:
            print(file_path)
            if file_path.endswith(".json"):
                continue  # Skip JSON files

            # ✅ Skip if already uploaded
            if file_path in state["uploaded_files"]:
                print(f"⚠️ Skipping {file_path}, already uploaded to S3.")
                continue

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            try:
                # Read file as binary
                content = sbx.files.read(file_path)
                if file_path in csv_sandbox_paths:
                    continue    
                if isinstance(content, str):
                    content = content.encode()  # Convert string to bytes if necessary
                print(file_path,"hello1")
                orig_file_name = os.path.basename(file_path)
                file_name = f"step{step_index+1}_{timestamp}_{orig_file_name}"
                print(file_path,"hello2")
                # Direct upload to S3
                s3_url = upload_to_s3_direct(content, file_name, S3_BUCKET_NAME)

                if s3_url:
                    state["final_response"] += f'\n✅ Uploaded to S3: {s3_url}'
                    await stream_to_frontend(chat_id, "bot_message", f'\n✅ Uploaded to S3: {s3_url}')
                    state["uploaded_files"][file_path] = s3_url  # Store S3 URL
                
                description = await get_json_and_generate_description(s3_url, S3_BUCKET_NAME, state)
                state["steps"].append(description)
                state["final_response"] += description or ""
                print("Final Description:", description)
            
            except Exception as e:
                print(f"⚠️ Error uploading {file_path}: {e}")
                await stream_to_frontend(chat_id, "bot_message", f"\n⚠️ Error uploading {file_path}: {e}")

        for idx, res in enumerate(result.results):
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # local_file_path = os.path.join(local_directory, f"step{step_index+1}_{timestamp}.png")  # Set filename per result
            #print(local_file_path)
            #print(res)
            if hasattr(res, "png") and res.png:
                # print("Inside hasattr", res, " ", res.png)
                # with open(local_file_path, "wb") as f:
                #     f.write(base64.b64decode(res.png))
                # print(type(res))  # Check the type of res
                # print(dir(res))   # List all attributes of res
                # print(type(res.data))  # Check the type of res
                # print(dir(res.data)) 
                # print(type(res.chart))  # Check the type of res
                # print(dir(res.chart),"hello")
                # print(res.chart.title,"hello2")
                # print(dir(res.chart.elements))

                png_bytes = base64.b64decode(res.png)
                if not isinstance(png_bytes, (bytes, bytearray)):
                    raise TypeError(f"Decoded content is not bytes. Got: {type(png_bytes)}")

                chart_title = getattr(res.chart, "title", f"Plot_{idx+1}")
                file_name = f"step{step_index+1}_{timestamp}_{chart_title}.png"
                s3_url = upload_to_s3_direct(png_bytes, file_name, S3_BUCKET_NAME)

                if s3_url:
                    state["final_response"] += f'\n✅ Uploaded directly to S3: {s3_url}'
                    await stream_to_frontend(chat_id, "bot_message", f'\n✅ Uploaded directly to S3: {s3_url}')

                description = await get_json_and_generate_description(s3_url, S3_BUCKET_NAME, state)
                state["steps"].append(description)
                print("Final Description:", description)
                state["final_response"] += description

            else:
                #print("else hasattr", res, " ", res.png)
                print(f'⚠️ No PNG found in result {idx+1}, skipping.')
                # await stream_to_frontend(chat_id, "bot_message", f'\n⚠️ No PNG found in result {idx+1}, skipping.')
    except Exception as e:
        print("Got an exception:", e)
        await stream_to_frontend(chat_id, "bot_message", f"\nGot an exception: {e}")
        state["execution_result"] = f"❌ Error executing code in E2B: {str(e)}"
        state["error"] = str(e)

    return state


# 🟢 Step 6: Auto Debug Python Code if Errors Exist
async def auto_debug_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    """Fixes Python errors using OpenAI and retries execution only if an error exists."""
    chat_id = state.get("chat_id")
    csv_info_text = "The following CSV files is/are available:\n"
    for csv_info in state["csv_info_list"]:
        csv_info_text += f"""
        - **File:** {csv_info['filename']}
        - **Path:** {csv_info['sandbox_path']}
        - **Columns:** {csv_info['column_names']}
        - **Rows:** {csv_info['num_rows']}
        - **Sample Data :**
        {csv_info['sample_data']}
    """
    if state["error"]:
        print("\n🔴 ERROR DETECTED! Asking LLM to fix the code...\n")
        await stream_to_frontend(chat_id, "bot_message", "\n🔴 ERROR DETECTED! Asking LLM to fix the code...\n")

        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        debug_prompt = f"""
        The original Python code was:
        ```
        {state['current_step_code']}
        ```
        It produced this error:
        ```
        {state['error']}


        ### 🔹 **General Debugging Rules**
        - If using `polars` and the error is related to `groupby`, replace `.groupby()` with `.group_by()` (Polars uses `.group_by()`, not `.groupby()`).
        - If `int64` or `float64` serialization errors occur (e.g., **Pydantic warnings**):
            - Convert `int64` → `int` using `.astype(int)` or `.item()`
            - Convert `float64` → `float` using `.astype(float)`
        - If **NaN or inf values** cause JSON issues, fix them:
            - `df.replace([np.inf, -np.inf], np.nan, inplace=True)`  # Replace infinite values
            - `df.fillna(0, inplace=True)`  # Fill missing values with 0 (or another default)
        if you get "can only concatenate str (not "NoneType") to str" as error/exception
        - If generating a plot with `matplotlib`, and you want the chart metadata to be picked up by the system (like `res.chart.title` in E2B): 
            - Set the plot title using `plt.title("<TITLE>")`
            - **Also set** `fig.suptitle("<TITLE>")` — this is what the system reads as the chart title.
            - Show using `plt.show()`

        The available CSV files/file have the following details & PATHS TO ACCES THEM ARE ALSO GIVEN, PLS USE THEM ONLY FURTHUR While debugging:


        ```
        The available CSV files/file have the following details & PATHS TO ACCCES THEM ARE ALSO GIVEN, PLS USE THEM ONLY FURTHUR While debugging:
        ```
        {csv_info_text}
        ```
        Fix the code. **STRICTLY return only valid Python code inside triple backticks (` ```python ... ``` `).** 
        Do NOT provide any explanations, comments, or additional text. Just return clean Python code.
        """

        print(debug_prompt)
        await stream_to_frontend(chat_id, "bot_message", state['error'])

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Fix the given Python code and return only the corrected code."},
                      {"role": "user", "content": debug_prompt}],
            stream=True
        )
        collected_text = ""
        async for chunk in response:
            if chunk.choices[0].delta.content is not None:
                partial_text = chunk.choices[0].delta.content
                print(partial_text, end="", flush=True)  # Stream to terminal
                collected_text += partial_text
                await stream_to_frontend(chat_id, "bot_message", partial_text)
        
        # print(response.choices[0].message.content)
        # ✅ Extract Python code using regex
        code_match = re.search(r"```python\n(.*?)```", collected_text, re.DOTALL)
        if code_match:
            fixed_code = code_match.group(1).strip()
            #print(f"Fixed Code :\n {fixed_code}")
        else:
            print("⚠️ Warning: LLM response did not contain a valid Python code block. Using raw response.")
            await stream_to_frontend(chat_id, "bot_message", "\n ⚠️ Warning: LLM response did not contain a valid Python code block. Using raw response.")
            fixed_code = response.choices[0].message.content.strip("```python").strip("```")

        state["current_step_code"] = fixed_code
        state["stepwise_code"][state["current_step_index"]]["code"] = fixed_code

        # state["execution_result"] = "✅ Code successfully debugged and ready for execution!"  
        state["error"] = None  

        return state

    else:
        print("✅ No errors detected. **Stopping execution.**")
        await stream_to_frontend(chat_id, "bot_message", "✅ No errors detected. **Stopping execution.**")
        return None  

# 🟢 Step 7: Decision Node to Check for Errors
async def check_for_errors(state: CodeInterpreterState) -> dict:
    if state["error"]:
        state["next"] = "auto_debug_python_code"
        return {"next": "auto_debug_python_code", "state": state}  # ✅ Return both next and updated state

    if state["current_step_index"] + 1 < len(state["stepwise_code"]):
        state["current_step_index"] += 1
        #print(state["current_step_index"], " This step index is getting updated now in check_for_errors")
        state["next"] = "generate_python_code"
        #print(state)
        return state  # ✅ Return both next and updated state

    state["next"] = "stop_execution"
    return {"next": "stop_execution", "state": state}  # ✅ Return both next and updated state


async def stop_execution(state: CodeInterpreterState) -> dict:
    """Stops execution."""
    chat_id = state.get("chat_id")
    print("🛑 Execution stopped successfully.")
    await stream_to_frontend(chat_id, "bot_message", "\n 🛑 Execution stopped successfully.")
    return {"status": "done", "execution_result": state["execution_result"]}

# ✅ Define Graph Flow
graph.add_node("extract_csv_info", extract_csv_info)
graph.add_node("generate_python_code", generate_python_code)
graph.add_node("generate_steps", generate_steps)
graph.add_node("execute_python_code", execute_python_code)
graph.add_node("auto_debug_python_code", auto_debug_python_code)
graph.add_node("check_for_errors", check_for_errors)
graph.add_node("stop_execution", stop_execution)

graph.set_entry_point("extract_csv_info")
graph.add_edge("extract_csv_info", "generate_steps")
graph.add_edge("generate_steps", "generate_python_code")  # First step
graph.add_edge("generate_python_code", "execute_python_code")
graph.add_edge("execute_python_code", "check_for_errors")
graph.add_edge("auto_debug_python_code", "execute_python_code")

graph.add_conditional_edges("check_for_errors", lambda state: state["next"], {
    "auto_debug_python_code": "auto_debug_python_code",
    "generate_python_code": "generate_python_code",  # For next step's code generation
    "stop_execution": "stop_execution",
})

from typing import List, Optional

class AttachedFile(BaseModel):
    file_name: str
    url: str

class Message(BaseModel):
    role: str
    text: str
    input_files: Optional[List] = []
    output_files: Optional[List] = []

class CodeInterpreterInput(BaseModel):
    user_id: str
    chat_id: str
    user_query: str
    attached_files: List[AttachedFile]
    last_5_messages: List[Message]
    sandbox_id: str
    csv_info_list: Optional[List[dict]] = []
    uploaded_files: Optional[dict] = {}  # ✅ Add this line to support uploaded_files
    steps: Optional[List[str]] = []  # ✅ Add this line



# ✅ Flask API Endpoint
@apponefast.post("/run")
async def run_langgraph(data: CodeInterpreterInput):
    try:
        summaries = []
        input_state = {
            "user_query": data.user_query,
            "csv_file_paths":[file.url for file in data.attached_files],
            "csv_info_list": data.csv_info_list if data.csv_info_list else [],  # ✅ Use incoming data if present
            "generated_code": "",
            "execution_result": "",
            "error": None,
            "sandbox": None,
            "current_step_index": 0,
            "current_step_code": "",
            "stepwise_code": [], # To store all step details (code, description, status)
            "uploaded_files": {},
            "final_response": "",
            "context": summaries,
            "user_id": data.user_id,
            "chat_id": data.chat_id,
            "steps": list(data.steps) if isinstance(data.steps, (list, tuple)) else [],
            "sandbox_id": data.sandbox_id,
            "uploaded_files": data.uploaded_files or {}
        }
        print(input_state," input_state")

        sbx = Sandbox.connect(data.sandbox_id)
        sbx.set_timeout(18000)


        # ✅ Invoke LangGraph
        executable_graph = graph.compile()
        output_state = await executable_graph.ainvoke(input_state, {"recursion_limit": 100})  # ✅ Fix

        await stream_to_frontend(data.chat_id, "completed_stream", "")

        print(output_state.get("steps", ""))

        print(output_state.get("csv_info_list"))

        return {
            "status": "success",
            "execution_result": output_state.get("execution_result", ""),  # ✅ Use .get() to avoid KeyErrors
            "error": output_state.get("error", None),
            "steps": output_state.get("steps", []),
            "code": output_state.get("generated_code", ""),
            "csv_info_list": output_state.get("csv_info_list"),
            "uploaded_files": input_state["uploaded_files"]  # ✅ return to frontend
        }
    

    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    

from fastapi import FastAPI
from e2b_code_interpreter import Sandbox

@apponefast.post("/create-sandbox")
async def create_sandbox():
    sandbox = Sandbox()
    return {"sandbox_id": sandbox.sandbox_id}


if __name__ == "__main__":
    uvicorn.run(apponefast, host="0.0.0.0", port=int(os.environ.get("PORT", 5006)), log_level="info")
