from typing import Optional
import yaml


def _extract_fenced_content(response: str, language: Optional[str] = None) -> Optional[str]:
    marker = f'```{language}' if language else '```'
    start = response.find(marker)
    if start == -1:
        return None

    start += len(marker)
    end = response.find('```', start)
    if end == -1:
        return None
    return response[start:end].strip()


def extract_code_from_response(response: str) -> Optional[str]:
    """从LLM响应中的YAML字段或代码围栏提取代码。"""
    yaml_content = _extract_fenced_content(response, 'yaml')
    if yaml_content is None:
        yaml_content = _extract_fenced_content(response)
    if yaml_content is None:
        yaml_content = response.strip()

    try:
        yaml_data = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        yaml_data = None

    if isinstance(yaml_data, dict) and 'code' in yaml_data:
        return yaml_data['code']

    python_content = _extract_fenced_content(response, 'python')
    if python_content is not None:
        return python_content
    return _extract_fenced_content(response)