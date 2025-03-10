import os
import shutil
from e2b_code_interpreter import Sandbox
import io
from flask import Flask
from flask import Flask, request, jsonify
import openai
import time
import requests
import pandas as pd
from typing import TypedDict
import json
import base64
import matplotlib.pyplot as plt
from PIL import Image
import re
from langgraph.graph import StateGraph
from flask_cors import CORS
import polars as pl
from flask_socketio import SocketIO, emit
import datetime
from flask_sock import Sock  # ✅ Use Flask-Sock instead of Flask-SocketIO
from flask import Flask, send_file, request, jsonify

S3_BUCKET_NAME = "code-interpreter-s3"

appone = Flask(__name__)
CORS(appone, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(appone, cors_allowed_origins="*", async_mode="threading")

# sock = Sock(appone)

@appone.route('/')
def serve_html():
    return send_file('chatinterface.html')  # Serve directly from root folder

def stream_to_frontend(event, message):
    try:
        socketio.emit({"event": event, "message": message})
        print(f"🔥 Emitting: {event} → {message}")
        # print(f"✅ Sent WebSocket message: {event} → {message}")
    except Exception as e:
        print(f"❌ Failed to send WebSocket message: {e}")

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


# 🟢 Step 2: Initialize Langgraph with State
graph = StateGraph(CodeInterpreterState)

# ✅ WebSocket Route
import threading
import time


def websocket(ws):
    print(f"📩 WebSocket received: {ws}")
    try:
        data = json.loads(ws) if isinstance(ws, str) else ws
    except json.JSONDecodeError:
        print("⚠️ Invalid JSON received.")
        return

    if data.get("event") == "ping":
        socketio.emit("pong", {"event": "pong"})  # Send pong response
        return

    if data.get("event") == "register_user":
        print(f"✅ User registered with ID: {data.get('user_id')}")

    # Echo message back for debugging
    socketio.emit("server_response", {"event": "server_response", "message": f"Echo: {data}"})


    # Echo response for testing
    socketio.emit("server_response", {"event": "server_response", "message": f"Echo: {data}"})



# 🟢 Step 3: Extract CSV Info and Upload to E2B
import polars as pl
import os
import sys
import polars as pl
import httpx
from io import BytesIO

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

import boto3

def upload_to_s3(file_path, bucket_name, s3_folder="results"):
    """ Upload a file to S3 and return the public URL. """
    s3_client = boto3.client('s3', region_name='us-east-2', aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"))  # Optional if using env variables)

    filename = os.path.basename(file_path)
    s3_key = f"{s3_folder}/{filename}"

    try:
        s3_client.upload_file(file_path, bucket_name, s3_key)
        s3_url = f"https://{bucket_name}.us-east-2.s3.amazonaws.com/{s3_key}"
        print(f"✅ Uploaded to S3: {s3_url}")
        return s3_url
    except Exception as e:
        print(f"❌ Failed to upload {file_path} to S3: {e}")
        return None

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


def extract_csv_info(state: CodeInterpreterState) -> CodeInterpreterState:
    try:
        # print("extract_csv_info (multiple CSV support)\n")
        sys.stdout.flush()

        sbx = Sandbox()
        sbx.commands.run("pip install polars")
        sbx.commands.run("pip install pyarrow")
        
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
            sys.stdout.flush()

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
            sys.stdout.flush()

            print(f"Finished Processing file: {csv_path}")

        state["csv_info_list"] = csv_info_list
        state["execution_result"] = "✅ All CSV files uploaded and info extracted"
        state["error"] = None

    except Exception as e:
        print("Got Exception ",e)
        state["execution_result"] = f"❌ Error processing CSV files: {str(e)}"
        state["error"] = str(e)

    return state

def generate_steps(state: CodeInterpreterState) -> CodeInterpreterState:
    stream_to_frontend("bot_message","The following CSV files is/are available:\n")
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
    You are a data analyst assistant. Based on the user's query and the available CSV files, generate a stepwise process for analyzing the data.
    The user query is:
    "{state['user_query']}"

    The available CSV files have the following details:
    {csv_info_text}

    ### Guidelines
    - Start with loading the CSV files. Always check the path provided in path provided earlier in the prompt (VVI).
    - Check for missing values and basic inspection, perform this if specified y the user query explicitly OTHERWISE DONT DO THIS.
    - Combine files if necessary.
    - Then, create steps tailored to the user query.
    - Ensure you only refer to columns that exist.

    ### Return Format
    - Only return the numbered list of steps (no explanations or code), DONT CREATE MORE THAN 4-5 STEPS.
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "You are a data analysis expert."},
                  {"role": "user", "content": steps_prompt}],
        temperature=0.2,
        stream=True
    )
    print("\n🔹 OpenAI Generated Steps:")
    stream_to_frontend("bot_message", "\n🔹 OpenAI Generated Steps:")
    collected_text = ""
    for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            # print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            stream_to_frontend("bot_message", partial_text)

    # steps_text = response.choices[0].message.content.strip()
    steps_text = collected_text
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
def generate_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    #print(state["current_step_index"], " This is our step index curently getting executed")
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    current_step = state["stepwise_code"][state["current_step_index"]]
    step_description = current_step["description"]

    print(f"\n🔵 Fetching Python code for Step {current_step['step']}: {step_description}")
    stream_to_frontend("bot_message", f"\n\n🔵 Fetching Python code for Step : {step_description}")

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

        User Query:
        {state['user_query']}

        You are currently working on **Step {state["current_step_index"]}: {step_description}**

        Please generate **ONLY Python code** for this step. Do not write any explanations or comments. Return only the code in a code block.
        The available CSV files/file have the following details & PATHS TO ACCCES THEM ARE ALSO GIVEN PLS USE THEM FURTHUR:
        {csv_info_text}
        Guidelines:
        - Use polars instead of pandas if the csv has more than 50000 rows,
        - ** If using datframes we usually get this error, keep this in mind, AttributeError: 'DataFrame' object has no attribute 'groupby' and dont use groupby with dataframes **
        - Use the correct sandbox file paths when reading the CSV files.
        - Use the same DataFrames across steps (assume they are already defined from previous steps).
        - Do not save matplotlib plots (just show them using plt.show()). Only save plotly visualizations using fig.write_html.
        - Any intermediate CSV files (like cleaned data) must be saved to /home/user/home/atharv/cleaned_step{state["current_step_index"]}.csv or similar.
        - Save the summary to /home/user/home/atharv/summary.txt.
    """
    print(step_prompt)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "You are a Python expert."},
                  {"role": "user", "content": step_prompt}],
        temperature=0.2,
        stream=True
    )
    collected_text = ""
    for chunk in response:
        if chunk.choices[0].delta.content is not None:
            partial_text = chunk.choices[0].delta.content
            print(partial_text, end="", flush=True)  # Stream to terminal
            collected_text += partial_text
            stream_to_frontend("bot_message", partial_text)

    # step_code = response.choices[0].message.content
    step_code = collected_text 
    code_match = re.search(r"```python\n(.*?)```", step_code, re.DOTALL)
    step_code_cleaned = code_match.group(1).strip() if code_match else step_code.strip()

    #print(step_code_cleaned)

    state["stepwise_code"][state["current_step_index"]]["code"] = step_code_cleaned
    state["current_step_code"] = step_code_cleaned

    #print(f"✅ Code fetched for Step {current_step['step']}")
    return state


# 🟢 Step 5: Execute Python Code in E2B Sandbox
def execute_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    """Uploads the generated script, executes it inside E2B Sandbox, and downloads result files if available."""
    #print("execute_python_code")
    try:
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
        stream_to_frontend("bot_message", f"\n🚀 Running Step {step_index + 1}")
        result = sbx.run_code(step_code)

        try:
         # ✅ Extract stdout correctly
            stdout_output = "\n".join(result.logs.stdout) if result.logs.stdout else "✅ Execution completed (no output)"
            # print(stdout_output)

            # ✅ Extract stderr correctly
            stderr_output = "\n".join(result.logs.stderr) if result.logs.stderr else None

            if stderr_output and "UserWarning" in stderr_output:
                print("⚠️ Detected UserWarning (not a fatal error), continuing execution.")
                stream_to_frontend("bot_message", "\n⚠️ Detected UserWarning (not a fatal error), continuing execution.")
                stderr_output = None  # Don't treat this as an error

            # print(stderr_output)
        

            #print("This is the result:", result)
            if result.error or result.logs.stderr:
                state["error"] = result.error.value if result.error else "\n".join(result.logs.stderr)
                state["stepwise_code"][step_index]["status"] = "failed"
                #print()
            else:
                state["stepwise_code"][step_index]["status"] = "success"
                state["error"] = None
                client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
                prompt = f"""
                    Please write a high-level, non-technical summary explaining the following process.
                    Assume the reader is a business user, not a developer.
                    Focus on explaining what data was analyzed, what visualizations were created, and any insights that might have been gained.
                    Here is the process:
                    {step_code}

                    Please keep the explanation clear and simple in 20-30 words.
                """
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "system", "content": "You are a code explainer."},
                            {"role": "user", "content": prompt}],
                    temperature=0.2,
                    stream=True
                )
                collected_text = ""
                for chunk in response:
                    if chunk.choices[0].delta.content is not None:
                        partial_text = chunk.choices[0].delta.content
                        print(partial_text, end="", flush=True)  # Stream to terminal
                        collected_text += partial_text
                        stream_to_frontend("bot_message", partial_text)
                
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
        stream_to_frontend("bot_message", f"\n📂 Files found in Sandbox: {cleaned_file_paths}")

        user_csv_path = cleaned_file_paths[0] if len(cleaned_file_paths) > 0 else ""
        #print(user_csv_path," user_csv_path")

        # ✅ Step 2: Download files
        # local_directory = "/Users/atharvwani/dloads"  # Change this to your preferred path
        # os.makedirs(local_directory, exist_ok=True)  # Ensure directory exists

        downloaded_files = []

        #print("This is result.results ",result.results)
        #print(len(result.results)," size")
        for idx, res in enumerate(result.results):
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # local_file_path = os.path.join(local_directory, f"step{step_index+1}_{timestamp}.png")  # Set filename per result
            #print(local_file_path)
            #print(res)
            if hasattr(res, "png") and res.png:
                # print("Inside hasattr", res, " ", res.png)
                # with open(local_file_path, "wb") as f:
                #     f.write(base64.b64decode(res.png))
                png_bytes = base64.b64decode(res.png)
                if not isinstance(png_bytes, (bytes, bytearray)):
                    raise TypeError(f"Decoded content is not bytes. Got: {type(png_bytes)}")

                file_name = f"step{step_index+1}_{timestamp}.png"
                s3_url = upload_to_s3_direct(png_bytes, file_name, S3_BUCKET_NAME)
                if s3_url:
                    stream_to_frontend("bot_message", f'\n✅ Uploaded directly to S3: {s3_url}')
            else:
                #print("else hasattr", res, " ", res.png)
                print(f'⚠️ No PNG found in result {idx+1}, skipping.')
                stream_to_frontend("bot_message", f'\n⚠️ No PNG found in result {idx+1}, skipping.')

        for file_path in cleaned_file_paths:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                # ✅ Read file as binary
                #print(file_path,"This is the file path")
                content = sbx.files.read(file_path)
                if user_csv_path == file_path:
                    #print(user_csv_path," user_csv_path in if")
                    continue
                if isinstance(content, str):
                    content = content.encode()  # Convert string to bytes if necessary
                
                # file_name = os.path.basename(file_path)
                original_extension = os.path.splitext(file_path)[1]
                file_name = f"step{step_index+1}_{timestamp}{original_extension}"
                # Direct upload to S3
                s3_url = upload_to_s3_direct(content, file_name, S3_BUCKET_NAME)

                if s3_url:
                    stream_to_frontend("bot_message", f'\n✅ Uploaded directly to S3: {s3_url}')
        

            except Exception as e:
                print(f"⚠️ Error downloading {file_path}: {e}")
                stream_to_frontend("bot_message", f"\n⚠️ Error downloading {file_path}: {e}")

        
    except Exception as e:
        print("Got an exception:", e)
        stream_to_frontend("bot_message", f"\nGot an exception: {e}")
        state["execution_result"] = f"❌ Error executing code in E2B: {str(e)}"
        state["error"] = str(e)

    return state


# 🟢 Step 6: Auto Debug Python Code if Errors Exist
def auto_debug_python_code(state: CodeInterpreterState) -> CodeInterpreterState:
    """Fixes Python errors using OpenAI and retries execution only if an error exists."""
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
        stream_to_frontend("bot_message", "\n🔴 ERROR DETECTED! Asking LLM to fix the code...\n")

        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        debug_prompt = f"""
        The original Python code was:
        ```
        {state['current_step_code']}
        ```
        It produced this error:
        ```
        {state['error']}
        ```
        The available CSV files/file have the following details & PATHS TO ACCCES THEM ARE ALSO GIVEN, PLS USE THEM ONLY FURTHUR While debugging:
        ```
        {csv_info_text}
        ```
        Fix the code. **STRICTLY return only valid Python code inside triple backticks (` ```python ... ``` `).** 
        Do NOT provide any explanations, comments, or additional text. Just return clean Python code.
        """

        print(debug_prompt)
        stream_to_frontend("bot_message", debug_prompt)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Fix the given Python code and return only the corrected code."},
                      {"role": "user", "content": debug_prompt}],
            stream=True
        )
        collected_text = ""
        for chunk in response:
            if chunk.choices[0].delta.content is not None:
                partial_text = chunk.choices[0].delta.content
                print(partial_text, end="", flush=True)  # Stream to terminal
                collected_text += partial_text
                stream_to_frontend("bot_message", partial_text)
        
        # print(response.choices[0].message.content)
        # ✅ Extract Python code using regex
        code_match = re.search(r"```python\n(.*?)```", collected_text, re.DOTALL)
        if code_match:
            fixed_code = code_match.group(1).strip()
            #print(f"Fixed Code :\n {fixed_code}")
        else:
            print("⚠️ Warning: LLM response did not contain a valid Python code block. Using raw response.")
            stream_to_frontend("bot_message", "\n ⚠️ Warning: LLM response did not contain a valid Python code block. Using raw response.")
            fixed_code = response.choices[0].message.content.strip("```python").strip("```")

        state["current_step_code"] = fixed_code
        state["stepwise_code"][state["current_step_index"]]["code"] = fixed_code

        # state["execution_result"] = "✅ Code successfully debugged and ready for execution!"  
        state["error"] = None  

        return state

    else:
        print("✅ No errors detected. **Stopping execution.**")
        stream_to_frontend("bot_message", "✅ No errors detected. **Stopping execution.**")
        return None  

# 🟢 Step 7: Decision Node to Check for Errors
def check_for_errors(state: CodeInterpreterState) -> dict:
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




def stop_execution(state: CodeInterpreterState) -> dict:
    """Stops execution."""
    print("🛑 Execution stopped successfully.")
    stream_to_frontend("bot_message", "\n 🛑 Execution stopped successfully.")
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


# ✅ Flask API Endpoint
@appone.route('/run', methods=['POST'])
def run_langgraph():
    try:
        data = request.get_json()

        if not data or "user_query" not in data or "csv_file_paths" not in data:
            return jsonify({"error": "Missing required fields: 'user_query' and 'csv_file_paths'"}), 400

        input_state = {
            "user_query": data["user_query"],
            "csv_file_paths": data["csv_file_paths"],  # Expecting list
            "csv_info_list": [],
            "generated_code": "",
            "execution_result": "",
            "error": None,
            "sandbox": None,
            "current_step_index": 0,
            "current_step_code": "",
            "stepwise_code": []  # To store all step details (code, description, status)

        }

        executable_graph = graph.compile()
        output_state = executable_graph.invoke(input_state,{"recursion_limit": 100})

        return jsonify({
            "status": "success",
            "execution_result": output_state["execution_result"],
            "error": output_state["error"],
            "code": output_state["generated_code"]
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ✅ Run Flask Server
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5006)) 
    socketio.run(appone, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)

    # appone.run(host="0.0.0.0", port=5006, debug=False, use_reloader=False)
# if __name__ == '__main__':
#     socketio.run(appone, host="localhost", port=5006, debug=True)
