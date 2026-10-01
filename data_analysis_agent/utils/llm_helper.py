# -*- coding: utf-8 -*-
"""
LLM调用辅助模块
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import yaml
from ..config.llm_config import LLMConfig
from .fallback_openai_client import AsyncFallbackOpenAIClient

class LLMHelper:
    """LLM调用辅助类，支持同步和异步调用"""
    
    def __init__(self, config: LLMConfig = None):
        self.config = config
    
    async def async_call(self, prompt: str, system_prompt: str = None, max_tokens: int = None, temperature: float = None, response_format=None) -> str:
        """异步调用LLM"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        kwargs = {}
        if max_tokens is not None:
            kwargs['max_tokens'] = max_tokens
        else:
            kwargs['max_tokens'] = self.config.max_tokens
            
        if temperature is not None:
            kwargs['temperature'] = temperature
        else:
            kwargs['temperature'] = self.config.temperature
        if self.config.reasoning_effort:
            kwargs['reasoning_effort'] = self.config.reasoning_effort
        if response_format is not None:
            kwargs['response_format'] = response_format
            
        client = AsyncFallbackOpenAIClient(
            primary_api_key=self.config.api_key,
            primary_base_url=self.config.base_url,
            primary_model_name=self.config.model
        )
        try:
            response = await client.chat_completions_create(
                messages=messages,
                **kwargs
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"LLM调用失败: {e}")
            return ""
        finally:
            await client.close()

    def call(self, prompt: str, system_prompt: str = None, max_tokens: int = None, temperature: float = None, response_format=None) -> str:
        """同步调用LLM"""
        def run_call():
            return asyncio.run(self.async_call(prompt, system_prompt, max_tokens, temperature, response_format))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run_call()

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(run_call).result()
    
    def parse_yaml_response(self, response: str) -> dict:
        """解析YAML格式的响应"""
        try:
            # 提取```yaml和```之间的内容
            if '```yaml' in response:
                start = response.find('```yaml') + 7
                end = response.find('```', start)
                yaml_content = response[start:end].strip()
            elif '```' in response:
                start = response.find('```') + 3
                end = response.find('```', start)
                yaml_content = response[start:end].strip()
            else:
                yaml_content = response.strip()
            
            parsed = yaml.safe_load(yaml_content)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            print(f"YAML解析失败: {e}")
            print(f"原始响应: {response}")
            return {}

    async def close(self):
        """兼容旧接口；客户端已在每次请求结束时关闭。"""
