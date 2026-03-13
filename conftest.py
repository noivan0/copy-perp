import pytest
import pytest_asyncio

# asyncio_mode=auto로 async fixture 자동 지원
pytest_plugins = ('pytest_asyncio',)
