"""Пометки для записей лога.

IMPORTANT - запись попадает в лог (и в S3) и вне окна работы, где обычно идёт только WARNING и выше:
    log.info("скрипт запущен", extra=IMPORTANT)
"""
IMPORTANT = {"important": True}
