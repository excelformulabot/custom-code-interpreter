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

# sock = Sock(appone)
import asyncio
from db import engine
from models import Base

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Run this once at startup
@apponefast.on_event("startup")
async def on_startup():
    await init_db()

from db import AsyncSessionLocal
from models import ChatMessage

async def save_chat_message(user_id: str, is_ai: bool, message: str, summary: str = ""):
    async with AsyncSessionLocal() as session:
        new_msg = ChatMessage(
            user_id=user_id,
            ai_message=is_ai,
            message=message,
            summary=summary
        )
        session.add(new_msg)
        await session.commit()

from sqlalchemy.future import select
from sqlalchemy import desc
from models import ChatMessage
from db import AsyncSessionLocal

async def get_last_ai_summaries(user_id: str, limit: int = 3):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id, ChatMessage.ai_message == True)
            .order_by(desc(ChatMessage.id))
            .limit(limit)
        )
        messages = result.scalars().all()
        return [msg.summary for msg in messages if msg.summary]


@apponefast.get("/")
async def serve_html():
    return FileResponse("chatinterface.html")

from typing import Dict

# ✅ Store active WebSocket connections per user_id
connected_users: Dict[str, WebSocket] = {}

# ✅ Function to send messages to a specific user
async def stream_to_frontend(user_id: str, event: str, message: str):
    """Send messages to a specific user's frontend via WebSocket."""
    print(f"Sending to {user_id}: {event} => {message[:30]}...")
    websocket = connected_users.get(user_id)
    if websocket:
        try:
            await websocket.send_text(json.dumps({"event": event, "message": message}))
        except Exception as e:
            print(f"❌ WebSocket Error for {user_id}: {e}")
            # Remove user on failure
            connected_users.pop(user_id, None)

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


# 🟢 Step 2: Initialize Langgraph with State
graph = StateGraph(CodeInterpreterState)

# ✅ WebSocket Route
global_websocket = None  # Stores the WebSocket connection

# ✅ WebSocket Route with user_id support
@apponefast.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connection for a specific user."""
    await websocket.accept()
    
    user_id = websocket.query_params.get("user_id")
    if not user_id:
        await websocket.close(code=4001)
        print("❌ No user_id provided. Connection closed.")
        return

    connected_users[user_id] = websocket
    print(f"✅ WebSocket connected: {user_id}")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("event") == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))

            elif message.get("event") == "bot_message":
                # You can handle incoming bot_message logic if needed
                print(f"📩 Received message from {user_id}: {message['message']}")

    except WebSocketDisconnect:
        print(f"❌ WebSocket Disconnected: {user_id}")
        connected_users.pop(user_id, None)


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

def download_file_to_sandbox(sbx: Sandbox, url: str, sandbox_folder: str = "home/atharv"):
    """ Download file from URL and write it directly to the sandbox. """
    filename = os.path.basename(url)

    # ✅ Fetch file from URL
    response = httpx.get(url)
    if response.status_code != 200:
        raise Exception(f"Failed to download {url}, status: {response.status_code}")

    # ✅ Write directly to sandbox without processing
    sandbox_path = f"{sandbox_folder}/{filename}"
    sbx.files.write(sandbox_path, response.content)

    # ✅ Return the sandbox path for reference
    return f"/home/user/{sandbox_path}"


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
    user_id = state.get("user_id")
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            # print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(user_id, "bot_message", partial_text)

    await stream_to_frontend(user_id, "bot_message", "\n \n")
    return collected_text

async def extract_csv_info(state: CodeInterpreterState) -> CodeInterpreterState:
    try:
        # print("extract_csv_info (multiple CSV support)\n")
        # sys.stdout.flush()

        sbx = Sandbox()
        sbx.commands.run("pip install polars")
        sbx.commands.run("pip install pyarrow")
        sbx.commands.run("pip install mpld3")
        
        
        state["sandbox"] = sbx

        csv_info_list = []
        sandbox_paths = []

        for csv_path in state["csv_file_paths"]:
            print(f"Processing file: {csv_path}")
    
            # Read with Polars
            # df = pl.read_csv(csv_path)
            if csv_path.startswith("http"):
                df = read_csv_from_url(csv_path)
            else:
                df = pl.read_csv(csv_path)

            print(f"Created Dataframes for file: {csv_path}")
            # Extract column names directly (Polars already gives a list)
            # print(df)
            column_names = df.columns

            # Number of rows
            num_rows = df.height  # 'height' is the row count in Polars

            # Data types as dictionary
            data_types = {col: str(dtype) for col, dtype in zip(df.columns, df.dtypes)}

            # Sample data (convert to Pandas just to generate string output)
            sample_data = df.head(3).to_pandas().to_string(index=False)

            print("Data gathering done")

            # Upload each CSV file to sandbox 
            # with open(csv_path, "rb") as f:
            if csv_path.startswith("http"):
                # Skip uploading for URLs
                sandbox_path = download_file_to_sandbox(sbx, csv_path)
                sandbox_paths.append(sandbox_path)
            else:
                # Upload to sandbox for local files
                with open(csv_path, "rb") as f:
                    sandbox_path = sbx.files.write(f"home/atharv/{os.path.basename(csv_path)}", f)
                sandbox_paths.append(f"/home/user/home/atharv/{os.path.basename(csv_path)}")

            print("Sandbox paths: ",sandbox_paths[-1])
            csv_info_list.append({
                "filename": os.path.basename(csv_path),
                "sandbox_path": sandbox_paths[-1],
                "column_names": column_names,
                "num_rows": num_rows,
                "data_types": data_types,
                "sample_data": sample_data
            })

            print(csv_info_list)
            # sys.stdout.flush()
            print(f"Finished Processing file: {csv_path}")

        state["csv_info_list"] = csv_info_list
        state["execution_result"] = "✅ All CSV files uploaded and info extracted"
        state["error"] = None
        for csv_info in csv_info_list:
            collected_text = await generate_csv_description(csv_info, state)
            state["final_response"] += collected_text

    except Exception as e:
        print("Got Exception ",e)
        state["execution_result"] = f"❌ Error processing CSV files: {str(e)}"
        state["error"] = str(e)

    return state

async def generate_steps(state: CodeInterpreterState) -> CodeInterpreterState:
    print("hello gs")
    # stream_to_frontend("bot_message","The following CSV files is/are available:\n")
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
        - Keep responses **natural, engaging, and interactive**, like a human assistant who is actively trying to help, keep steps in first person.
        - Avoid redundancy by ensuring each step serves a unique purpose. 
        - Vary phrasing to avoid repetition. Instead of starting every step with "I will do this," make it dynamic—sometimes say "Let me check that," "Next, I'll handle...," or "To make sure we get the best results, I'll also..."
        - Responses should **show effort and care** in solving the user's query.
        - These generated steps are later translated into Python code—keep this in mind to ensure efficient execution.
        - Do not generate a step which would result in python code which would do the same code, You need to understand that these steps generate python code and i later run that python code in a sandbox where i dont want any two steps doing same work.

        **Previous context (summaries of earlier tasks, use this in response if required):**
        {chr(10).join(f"- {ctx}" for ctx in state.get("context", []) if ctx)}

        **User Query:**
        "{state['user_query']}"

        **Available CSV Files:**
        {csv_info_text}

        **Guidelines for Step Generation:**
        - **If the query requires a quick factual lookup** (e.g., row count, column names, basic stats), **generate only 1 step**.
        - **For slightly more involved queries** (e.g., filtering, computing averages, grouping data), **generate at most 2 steps**.
        - **DO NOT create extra steps for naming or verifying file saves unless explicitly requested.**
        - **DO NOT break down simple operations into unnecessary steps.**
          - If an action can be performed in **one command** (e.g., extracting a column and saving it to CSV), **combine them into a single step**.
        - **No intermediate steps for creating a new DataFrame unless explicitly required.**
        - **No repeated steps for saving a file with different names. Only one final saved output is allowed.**
        - **Avoid unnecessary validation steps** (e.g., confirming a file was saved) unless explicitly required.
        - If multiple steps are necessary, ensure **each step contributes uniquely** to the solution. No two steps should perform the same or highly similar actions.
        - Use the above csv sample data to know what columns are there. You dont need a step to get an overview of data structure.
        - **Keep it concise yet meaningful.** If the query requires only a **quick factual lookup** (e.g., count rows, get column names, calculate mean), generate **just 1-2 steps.**
        - **Start with loading the necessary CSV files**, always ensuring the correct path is used from the provided details.
        - **Check for missing values and perform basic inspection ONLY if explicitly requested**—do not add this step unnecessarily.
        - **Merge datasets if required** to complete the query effectively.
        - **The steps should align with how Python code will be generated later**, ensuring they lead to efficient execution.
        - **Do not exceed 4-5 steps**, but also do not force a fixed number—adjust based on query complexity.

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
    user_id = state.get("user_id")
    print(user_id," This is the user id")
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            # print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(user_id, "bot_message", partial_text)

    # steps_text = response.choices[0].message.content.strip()
    steps_text = collected_text
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
    user_id = state.get("user_id")
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    current_step = state["stepwise_code"][state["current_step_index"]]
    step_description = current_step["description"]

    print(f"\n🔵 Fetching Python code for Step {current_step['step']}: {step_description}")
    await stream_to_frontend(user_id, "bot_message", f"\n\n🔵 Fetching Python code ... \n\n Step : {step_description}\n\n")

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
        
        **Previous context (summaries of earlier tasks, use this in response if required):**
        {chr(10).join(f"- {ctx}" for ctx in state.get("context", []) if ctx)}

        The available CSV files/file have the following details & PATHS TO ACCCES THEM ARE ALSO GIVEN PLS USE THEM FURTHUR:
        {csv_info_text}
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
            await stream_to_frontend(user_id, "bot_message", partial_text)

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
    user_id = state.get("user_id")
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
    await stream_to_frontend(user_id, "bot_message", '\n')
    collected_text = ""
    async for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            await stream_to_frontend(user_id, "bot_message", partial_text)
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
        user_id = state.get("user_id")
        state["error"] = None  # Reset any previous errors

        # ✅ Retrieve the Sandbox instance
        sbx = state.get("sandbox")
        if not sbx:
            raise ValueError("Sandbox instance not found in state.")

        #print(f"Executing in Sandbox with path: {state['csv_file_paths']}")
        #print(state["generated_code"],"This is the code going") 
        # result = sbx.run_code(state["generated_code"])
        step_index = state["current_step_index"]
        step_code = state["current_step_code"]
        print(f"🚀 Running Step {step_index + 1}")
        await stream_to_frontend(user_id, "bot_message", f"\n🚀 Running Step {step_index + 1} in the Sandbox......\n")
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
                await stream_to_frontend(user_id, "bot_message", "\n⚠️ Detected UserWarning (not a fatal error), continuing execution.")
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
                await stream_to_frontend(user_id, "bot_message", "\n⚠️ Detected Error (fatal error), not continuing execution.")
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



            await stream_to_frontend(user_id, "bot_message", f"\n🚀 Execution Completed in Sandbox\n\n")

            s3_url = None  # Initialize before using it
            for log in result.logs.stdout:
                print(log)
                if is_plain_text(log):
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    file_name = f"step{step_index+1}_{timestamp}.txt"

                    # ✅ Upload to S3
                    s3_url = upload_to_s3_direct(log.encode(), file_name, S3_BUCKET_NAME)

                    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
                    await stream_to_frontend(user_id, "bot_message", f"\n")
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
                    await stream_to_frontend(user_id, "bot_message", '\n')
                    collected_text = ""
                    async for chunk in response:
                        if chunk.choices[0].delta.content is not None:
                            partial_text = chunk.choices[0].delta.content
                            print(partial_text, end="", flush=True)  # Stream to terminal
                            collected_text += partial_text
                            await stream_to_frontend(user_id, "bot_message", partial_text)
                    print(f"Generated Description: {collected_text}")
                    state["final_response"] += collected_text

                if s3_url:
                    await stream_to_frontend(user_id, "bot_message", f'\n✅ Output saved: {s3_url}\n')
                    state["final_response"] += f'\n✅ Output saved: {s3_url}\n'

            #print("This is the result:", result)

                #print()
            state["stepwise_code"][step_index]["status"] = "success"
            state["error"] = None
            client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            prompt = f"""
                Please write a high-level, non-technical summary explaining the following process.
                Assume the reader is a business user, not a developer.
                Focus on explaining what data was analyzed, what visualizations were created, and any insights that might have been gained.
                Here is the process:
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
                    await stream_to_frontend(user_id, "bot_message", partial_text)
            
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
        raw_output = dir_result.logs.stdout if dir_result.logs.stdout else []
        cleaned_file_paths = [path.strip() for path in raw_output[0].split("\n") if path.strip()]

        print("📂 Files found in Sandbox:", cleaned_file_paths)
        # await stream_to_frontend(user_id, "bot_message", f"\n📂 Files found in Sandbox: {cleaned_file_paths}")

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
                    # await stream_to_frontend(user_id, "bot_message", f'\n✅ Uploaded JSON to S3: {s3_url}')
            
            except Exception as e:
                print(f"⚠️ Error uploading {file_path}: {e}")
                await stream_to_frontend(user_id, "bot_message", f"\n⚠️ Error uploading {file_path}: {e}")

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
                    await stream_to_frontend(user_id, "bot_message", f'\n✅ Uploaded to S3: {s3_url}')
                    state["uploaded_files"][file_path] = s3_url  # Store S3 URL
                
                description = await get_json_and_generate_description(s3_url, S3_BUCKET_NAME, state)
                state["final_response"] += description
                print("Final Description:", description)
            
            except Exception as e:
                print(f"⚠️ Error uploading {file_path}: {e}")
                await stream_to_frontend(user_id, "bot_message", f"\n⚠️ Error uploading {file_path}: {e}")

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

                file_name = f"step{step_index+1}_{timestamp}_{res.chart.title}.png"
                s3_url = upload_to_s3_direct(png_bytes, file_name, S3_BUCKET_NAME)

                if s3_url:
                    state["final_response"] += f'\n✅ Uploaded directly to S3: {s3_url}'
                    await stream_to_frontend(user_id, "bot_message", f'\n✅ Uploaded directly to S3: {s3_url}')

                description = await get_json_and_generate_description(s3_url, S3_BUCKET_NAME, state)
                print("Final Description:", description)
                state["final_response"] += description

            else:
                #print("else hasattr", res, " ", res.png)
                print(f'⚠️ No PNG found in result {idx+1}, skipping.')
                # await stream_to_frontend(user_id, "bot_message", f'\n⚠️ No PNG found in result {idx+1}, skipping.')
    except Exception as e:
        print("Got an exception:", e)
        await stream_to_frontend(user_id, "bot_message", f"\nGot an exception: {e}")
        state["execution_result"] = f"❌ Error executing code in E2B: {str(e)}"
        state["error"] = str(e)

    return state


# 🟢 Step 6: Auto Debug Python Code if Errors Exist
async def auto_debug_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    """Fixes Python errors using OpenAI and retries execution only if an error exists."""
    user_id = state.get("user_id")
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
        await stream_to_frontend(user_id, "bot_message", "\n🔴 ERROR DETECTED! Asking LLM to fix the code...\n")

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
            ```python
            df.replace([np.inf, -np.inf], np.nan, inplace=True)  # Replace infinite values
            df.fillna(0, inplace=True)  # Fill missing values with 0 (or another default)

        ```
        The available CSV files/file have the following details & PATHS TO ACCCES THEM ARE ALSO GIVEN, PLS USE THEM ONLY FURTHUR While debugging:
        ```
        {csv_info_text}
        ```
        Fix the code. **STRICTLY return only valid Python code inside triple backticks (` ```python ... ``` `).** 
        Do NOT provide any explanations, comments, or additional text. Just return clean Python code.
        """

        print(debug_prompt)
        await stream_to_frontend(user_id, "bot_message", state['error'])

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
                await stream_to_frontend(user_id, "bot_message", partial_text)
        
        # print(response.choices[0].message.content)
        # ✅ Extract Python code using regex
        code_match = re.search(r"```python\n(.*?)```", collected_text, re.DOTALL)
        if code_match:
            fixed_code = code_match.group(1).strip()
            #print(f"Fixed Code :\n {fixed_code}")
        else:
            print("⚠️ Warning: LLM response did not contain a valid Python code block. Using raw response.")
            await stream_to_frontend(user_id, "bot_message", "\n ⚠️ Warning: LLM response did not contain a valid Python code block. Using raw response.")
            fixed_code = response.choices[0].message.content.strip("```python").strip("```")

        state["current_step_code"] = fixed_code
        state["stepwise_code"][state["current_step_index"]]["code"] = fixed_code

        # state["execution_result"] = "✅ Code successfully debugged and ready for execution!"  
        state["error"] = None  

        return state

    else:
        print("✅ No errors detected. **Stopping execution.**")
        await stream_to_frontend(user_id, "bot_message", "✅ No errors detected. **Stopping execution.**")
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
    user_id = state.get("user_id")
    print("🛑 Execution stopped successfully.")
    await stream_to_frontend(user_id, "bot_message", "\n 🛑 Execution stopped successfully.")
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

class CodeInterpreterInput(BaseModel):
    user_query: str
    csv_file_paths: List[str]
    user_id: str

# ✅ Flask API Endpoint
@apponefast.post("/run")
async def run_langgraph(data: CodeInterpreterInput):
    try:
        # await save_chat_message(user_id="user123", is_ai=False, message="How many survived?", summary="")
        await save_chat_message(user_id="user123", is_ai=True, message=data.user_query, summary="")
        summaries = await get_last_ai_summaries("user123")
        input_state = {
            "user_query": data.user_query,
            "csv_file_paths": data.csv_file_paths,
            "csv_info_list": [],
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
            "user_id": data.user_id
        }
        print(input_state," input_state")

        # ✅ Invoke LangGraph
        executable_graph = graph.compile()
        output_state = await executable_graph.ainvoke(input_state, {"recursion_limit": 100})  # ✅ Fix
        
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        prompt = f"""
        You are a summarization assistant, You are an assistant helping to build contextual memory for a CSV analysis chatbot.

        Given the full text output below, your job is to produce a clean, structured summary of what was done to answer the user’s query.

        Focus on these rules:

        ---

        🟢 **SECTION 1: Summary of what was done**

        - Describe **in 3–4 lines** what was done to solve the query — only mention actions like:
        - If a file was generated (what and why)
        - If any values were calculated (what values and for what purpose)
        - If plots were created (what they show, what was discovered)

        - DO NOT mention individual steps or say "the AI did" — this is a user-facing summary.
        - Keep it outcome-focused: What was analyzed, what was generated, and what key insights were uncovered.

        ---

        🟢 **SECTION 2: List of important values discovered** (only if any were printed or inferred)

        - Bullet points of important insights like:
        - Survival rates by class or gender
        - Averages, counts, or major numeric outcomes
        - Differences or comparisons (e.g., females survived more than males)

        ---

        🟢 **SECTION 3: All files that were generated**

        - For each file:
        - File name  
        - Purpose or what it contains  
        - S3 link or sandbox path

        Use this format:

        Files Generated:

        numerical_summary.csv: Summary stats for Age and Fare
        📍 Path: /home/user/home/atharv/numerical_summary.csv
        🔗 S3: https://...

        Survival Rates by Gender.png: Bar chart visualizing survival by gender
        📍 Path: /home/user/home/atharv/...
        🔗 S3: https://...


        Now, generate the summary using the text below: \n {output_state.get("final_response")}

        """

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": " You are an assistant helping to build contextual memory for a CSV analysis chatbot."},
                      {"role": "user", "content": prompt}],
        )
        # print(output_state.get("final_response")," hellorvce")
        # print(response.choices[0].message.content)
        await save_chat_message(user_id="user123", is_ai=True, message=output_state.get("final_response"), summary=response.choices[0].message.content)
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
        sbx = output_state.get("sandbox")
        dir_result = sbx.run_code(list_files_script)

        # ✅ Extract and clean the list of file paths
        raw_output = dir_result.logs.stdout if dir_result.logs.stdout else []
        cleaned_file_paths = [path.strip() for path in raw_output[0].split("\n") if path.strip()]

        print("📂 Files found in Sandbox:", cleaned_file_paths)

        return {
            "status": "success",
            "execution_result": output_state.get("execution_result", ""),  # ✅ Use .get() to avoid KeyErrors
            "error": output_state.get("error", None),
            "code": output_state.get("generated_code", "")
        }
    
        

    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ✅ Run Flask Server
# if __name__ == '__main__':
#     port = int(os.environ.get("PORT", 5006)) 
#     socketio.run(appone, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)

    # appone.run(host="0.0.0.0", port=5006, debug=False, use_reloader=False)
# if __name__ == '__main__':
#     socketio.run(appone, host="localhost", port=5006, debug=True)

if __name__ == "__main__":
    uvicorn.run(apponefast, host="0.0.0.0", port=int(os.environ.get("PORT", 5006)), log_level="info")
