"""集中配置：所有模块从 get_settings() 读取，禁止散落 os.environ。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 允许从仓库根目录或 backend/ 目录启动，都能读到根目录的 .env
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(str(_ROOT_ENV), ".env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    version: str = "0.1.0"

    fuyao_api_key: str | None = None
    fuyao_base_url: str = "https://fuyao.aicubes.cn"
    fuyao_max_concurrency: int = 6
    fuyao_rps: float = 8.0
    fuyao_timeout_s: float = 20.0

    ifind_mcp_token: str | None = None
    ifind_mcp_url: str = "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-mcp"
    ifind_timeout_s: float = 20.0

    llm_api_key: str | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout_s: float = 40.0

    data_dir: str = "./data"
    demo_faults_enabled: bool = True
    warm_universes: str = "000300.SH"  # 逗号分隔，启动后后台预热
    max_deep_eval: int = 800  # 单次深度评估（逐股取财务/K线）的股票数上限

    @property
    def fuyao_enabled(self) -> bool:
        return bool(self.fuyao_api_key)

    @property
    def ifind_enabled(self) -> bool:
        return bool(self.ifind_mcp_token)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
