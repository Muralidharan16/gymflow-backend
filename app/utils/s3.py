import os
import io
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from app.core.config import settings

class LocalS3Client:
    def __init__(self):
        self.base_dir = os.path.join(os.getcwd(), "storage", settings.S3_BUCKET_NAME)
        os.makedirs(self.base_dir, exist_ok=True)

    def generate_presigned_post(self, Bucket, Key, Fields=None, Conditions=None, ExpiresIn=300):
        # Points directly to the FastAPI local mock S3 upload endpoint
        url = f"{settings.BACKEND_BASE_URL}/organizations/mock-s3/upload"
        fields = Fields or {}
        fields["key"] = Key
        return {
            "url": url,
            "fields": fields
        }

    def get_object(self, Bucket, Key):
        file_path = os.path.join(self.base_dir, Key)
        if not os.path.exists(file_path) or os.path.isdir(file_path):
            raise ClientError(
                error_response={"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
                operation_name="GetObject"
            )
        with open(file_path, "rb") as f:
            data = f.read()
        return {
            "Body": io.BytesIO(data),
            "ContentType": "image/png"
        }

    def put_object(self, Bucket, Key, Body, ContentType=None):
        file_path = os.path.join(self.base_dir, Key)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        if isinstance(Body, bytes):
            with open(file_path, "wb") as f:
                f.write(Body)
        elif hasattr(Body, "read"):
            with open(file_path, "wb") as f:
                f.write(Body.read())
        else:
            with open(file_path, "wb") as f:
                f.write(str(Body).encode())
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        file_path = os.path.join(self.base_dir, Key)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(Fileobj.read())
        return True

    def delete_object(self, Bucket, Key):
        file_path = os.path.join(self.base_dir, Key)
        if os.path.exists(file_path) and not os.path.isdir(file_path):
            os.remove(file_path)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def list_objects_v2(self, Bucket, Prefix=""):
        search_dir = os.path.join(self.base_dir, Prefix)
        contents = []
        if os.path.exists(self.base_dir):
            for root, _, files in os.walk(self.base_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_key = os.path.relpath(full_path, self.base_dir)
                    if rel_key.startswith(Prefix):
                        contents.append({
                            "Key": rel_key,
                            "Size": os.path.getsize(full_path)
                        })
        return {"Contents": contents}
    
    def get_paginator(self, operation_name):
        class LocalPaginator:
            def __init__(self, client):
                self.client = client
            def paginate(self, Bucket, Prefix=""):
                res = self.client.list_objects_v2(Bucket, Prefix)
                return [res]
        return LocalPaginator(self)

def get_s3_client():
    if settings.ENVIRONMENT == "development":
        return LocalS3Client()
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION_NAME,
        config=Config(signature_version='s3v4')
    )
