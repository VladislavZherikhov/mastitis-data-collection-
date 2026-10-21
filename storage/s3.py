"""Подключение к Yandex Object Storage и загрузка снимков."""
import logging
import time

import boto3
from botocore.config import Config

from core import config
from core.logmarks import IMPORTANT

log = logging.getLogger("s3")

# коды ошибок S3, которые сами не пройдут - нужно вмешательство человека
_AUTH_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "Forbidden", "403",
               "InvalidToken", "ExpiredToken"}
_BUCKET_CODES = {"NoSuchBucket", "404", "NotFound"}
_CLOCK_CODES = {"RequestTimeTooSkewed", "RequestExpired"}


def classify_error(e: Exception) -> tuple[str, str]:
    """Вид ошибки S3 и понятное описание.

    network - нет связи/таймаут (пройдёт сама), auth - ключи, bucket - нет бакета,
    clock - часы Pi расходятся с реальным временем, other - что-то ещё."""
    code = ""
    response = getattr(e, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
    if code in _AUTH_CODES:
        return "auth", f"S3 отклоняет ключи доступа ({code}) - проверь S3_KEY_ID/S3_SECRET и права ключа"
    if code in _BUCKET_CODES:
        return "bucket", f"бакет {config.S3_BUCKET} не найден ({code}) - проверь S3_BUCKET"
    if code in _CLOCK_CODES:
        return "clock", f"S3 отклоняет запросы из-за неверного времени на Pi ({code}) - проверь синхронизацию времени"
    if code:
        return "other", f"S3 вернул ошибку {code}: {e}"
    name = type(e).__name__
    if any(s in name for s in ("Connect", "Timeout", "Endpoint", "Connection", "Proxy", "SSL")) \
            or isinstance(e, (OSError, TimeoutError)):
        return "network", f"нет связи с S3 ({name}: {e})"
    return "other", f"ошибка S3 ({name}: {e})"


class S3Storage:
    def __init__(self):
        key_id, secret = config.s3_credentials()
        self.bucket = config.S3_BUCKET
        self.prefix = config.S3_PREFIX.strip("/")
        # boto3-клиент потокобезопасен, поэтому один экземпляр можно использовать из нескольких потоков.
        # Таймауты небольшие: при плохой связи лучше быстро сдаться (пакет останется на диске
        # и уйдёт позже), чем держать поток загрузки минутами
        self._client = boto3.client(
            "s3",
            endpoint_url=config.S3_ENDPOINT,
            region_name=config.S3_REGION,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            config=Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def check(self) -> bool:
        """Проверка доступа к бакету. Не падает: без сети скрипт всё равно должен начать снимать."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception as e:
            kind, text = classify_error(e)
            level = logging.WARNING if kind == "network" else logging.ERROR
            log.log(level, "проверка доступа к бакету не прошла: %s. Продолжаю работу - пакеты сохраняются "
                           "на диск и уйдут в S3, когда доступ появится", text)
            return False
        log.info("доступ к бакету %s есть (%s)", self.bucket, config.S3_ENDPOINT, extra=IMPORTANT)
        return True

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        started = time.monotonic()
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        log.debug("S3: %s (%.0f КБ) за %.2f с", key, len(data) / 1024, time.monotonic() - started)

    def upload_shot(self, folder: str, files: dict[str, tuple[bytes, str]]) -> str:
        """Грузит набор файлов в <prefix>/<folder>/.

        files: {"имя_файла": (байты, content_type)}
        Возвращает s3-URI папки. Если загрузилась только часть файлов, пробует удалить
        загруженное, чтобы в облаке не оставалось неполных пакетов (полный пакет уйдёт позже с диска).
        """
        base = f"{self.prefix}/{folder}" if self.prefix else folder
        done: list[str] = []
        try:
            for name, (data, content_type) in files.items():
                key = f"{base}/{name}"
                self.upload_bytes(key, data, content_type)
                done.append(key)
        except Exception:
            if done:
                log.debug("S3: пакет %s загружен частично (%d из %d файлов) - удаляю загруженное",
                          folder, len(done), len(files))
                self._delete_quietly(done)
            raise
        return f"s3://{self.bucket}/{base}/"

    def _delete_quietly(self, keys: list[str]) -> None:
        try:
            self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys]})
        except Exception as e:
            # связи нет - неполный пакет останется в облаке; если пакет есть на диске,
            # догрузка позже перезапишет его целиком
            log.warning("S3: не удалось удалить неполный пакет (%s): %s", ", ".join(keys), e)
