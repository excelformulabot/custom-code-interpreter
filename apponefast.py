from fastapi import FastAPI, Request
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import os
import requests
import boto3
from io import BytesIO

# S3 setup (fill these with your actual values)

bucket_name = "code-interpreter-s3"
region = "us-east-2"
bucket_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/"
app = FastAPI()
s3 = boto3.client("s3", region_name=region)

def upload_to_s3_direct(content: bytes, file_name: str, bucket_name: str, s3_folder="code"):
    s3_client = boto3.client(
        's3',
        region_name=region,
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
        s3_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{s3_key}"
        print(f"✅ Uploaded: {s3_url}")
        return s3_url
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        return None



class CodeExecutionRequest(BaseModel):
    code: str
    sandbox_id: str | None = None

@app.post("/create-sandbox")
async def create_sandbox():
    sbx = Sandbox()
    return {"sandbox_id": sbx.sandbox_id}

uploaded_pngs = set()
@app.post("/execute-code")
async def execute_code(data: CodeExecutionRequest):
    try:
        sandbox = Sandbox.connect(data.sandbox_id)
        sandbox.set_timeout(6000)

        result = sandbox.run_code(data.code)

        import base64

        markdown_images = []
        stdout = "\n".join(result.logs.stdout) if result.logs.stdout else ""
        stderr = "\n".join(result.logs.stderr) if result.logs.stderr else ""

        # 1️⃣ Upload PNGs from result.results
        for idx, res in enumerate(result.results):
            if hasattr(res, "png") and res.png:
                try:
                    png_bytes = base64.b64decode(res.png)
                    file_name = f"plot{idx+1}.png"
                    s3_url = upload_to_s3_direct(png_bytes, file_name, bucket_name)
                    uploaded_pngs.add(file_name)  
                    if s3_url:
                        markdown_images.append(f"![]({s3_url})")
                except Exception as e:
                    print(f"⚠️ Failed to upload plot{idx+1}.png: {e}")

        # 2️⃣ Upload and delete other files from /code
        uploaded_files = sandbox.files.list("/code")
        for file in uploaded_files:
            if file.name.endswith(".png") and file.name in uploaded_pngs:
                continue  # Already handled above

            try:
                content = sandbox.files.read(file.path)
                filename = os.path.basename(file.path)

                if isinstance(content, str):
                    content = content.encode()

                s3_url = upload_to_s3_direct(content, filename, bucket_name, '')

                if s3_url:
                    if file.name.endswith(".csv"):
                        markdown_images.append(
                            f"{file.name} download link:\n{s3_url}\n!"
                        )
                    else:
                        markdown_images.append(f"![]({s3_url})" if file.name.endswith(".png") else f"\n📄 [{file.name}]({s3_url})")

                    # ✅ Delete file from sandbox after successful upload
                    delete_code = f"import os\nos.remove('{file.path}')"
                    try:
                        sandbox.run_code(delete_code)
                        print(f"🗑️ Deleted {file.path} from sandbox")
                    except Exception as e:
                        print(f"⚠️ Failed to delete {file.path}: {e}")

            except Exception as e:
                print(f"⚠️ Failed to upload {file.name}: {e}")

        final_output = stdout + "\n" + "\n".join(markdown_images)

        return {
            "sandbox_id": sandbox.sandbox_id,
            "stdout": final_output,
            "stderr": stderr,
            "error": {
                "name": result.error.name if result.error else None,
                "message": result.error.value if result.error else None,
                "traceback": result.error.traceback.splitlines() if result.error and result.error.traceback else None
            }
        }

    except Exception as e:
        return {"error": str(e)}


# uvicorn new:app --reload --port 5006
