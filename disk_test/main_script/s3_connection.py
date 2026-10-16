"""Подключение к Yandex Object Storage и загрузка снимков."""
import logging

import boto3
from botocore.config import Config

import config

log = logging.getLogger("s3")


class S3Storage:
    def __init__(self):
        key_id, secret = config.s3_credentials()
        self.bucket = config.S3_BUCKET
        self.prefix = config.S3_PREFIX.strip("/")
        # boto3-клиент потокобезопасен, поэтому один экземпляр можно использовать из нескольких потоков
        self._client = boto3.client(
            "s3",
            endpoint_url=config.S3_ENDPOINT,
            region_name=config.S3_REGION,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def check(self) -> None:
        """Проверка доступа к бакету (вызывать один раз на старте)."""
        self._client.head_bucket(Bucket=self.bucket)
        log.info("доступ к бакету %s есть", self.bucket)

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def upload_shot(self, folder: str, files: dict[str, tuple[bytes, str]]) -> str:
        """Грузит набор файлов в <prefix>/<folder>/.

        files: {"имя_файла": (байты, content_type)}
        Возвращает s3-URI папки.
        """
        base = f"{self.prefix}/{folder}" if self.prefix else folder
        for name, (data, content_type) in files.items():
            self.upload_bytes(f"{base}/{name}", data, content_type)
        return f"s3://{self.bucket}/{base}/"
