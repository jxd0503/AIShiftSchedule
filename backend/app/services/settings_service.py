"""设置域应用服务：模型接口配置、连接测试与软约束权重。"""
from __future__ import annotations

from typing import Any

from sqlmodel import Session

from ..db import engine
from ..errors import DomainError
from . import llm_service
from .soft_weights import load_soft_weights, save_soft_weights, weight_items


def load_settings() -> dict[str, Any]:
    """读取模型配置与软约束权重，供设置页展示。"""
    with Session(engine) as session:
        values = llm_service.get_settings(session)
        return {**values, "soft_weights": weight_items(load_soft_weights(session))}


def save_settings(
    base_url: str,
    model: str,
    api_key: str,
    soft_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """保存模型配置（可选同时保存权重）；校验失败抛 DomainError。"""
    base_url = base_url.strip()
    model = model.strip()
    api_key = api_key.strip()
    if not base_url.startswith(("http://", "https://")):
        raise DomainError("接口地址必须以 http:// 或 https:// 开头")
    if not model:
        raise DomainError("模型名称不能为空")
    with Session(engine) as session:
        values = llm_service.save_settings(session, base_url, model, api_key)
        if soft_weights:
            save_soft_weights(session, soft_weights)
        return {**values, "soft_weights": weight_items(load_soft_weights(session))}


def test_connection(base_url: str, model: str, api_key: str) -> tuple[str, float]:
    """发送一次极短对话验证连通性，返回 (模型回复, 耗时秒数)。"""
    if not api_key.strip():
        raise DomainError("请先填写 API Key")
    try:
        return llm_service.test_connection(base_url, model, api_key)
    except Exception as error:  # noqa: BLE001 - 网络与鉴权异常统一转业务错误
        raise DomainError(f"连接失败：{error}") from error
