"""Делает bridge.py импортируемым из тестов (bridge/ в sys.path) и изолирует состояние.

Изоляция не формальность: тест, забывший указать points_file, писал в БОЕВОЙ файл
состояния пользователя и обнулил ему счёт. Дефолтный путь берётся из окружения, поэтому
достаточно подменить его один раз на весь прогон — тогда ни одна фикстура не сможет
дотянуться до реального файла, даже если про него забыли.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

# До импорта bridge: Config читает окружение при создании, а модуль импортируется
# тестами сразу же.
_STATE = tempfile.mkdtemp(prefix="octodash-tests-")
os.environ.setdefault("OCTO_POINTS_FILE", os.path.join(_STATE, "points.json"))
os.environ.setdefault("OCTO_LOG_FILE", "-")          # и лог мимо боевого файла
