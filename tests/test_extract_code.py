from data_analysis_agent.utils.extract_code import extract_code_from_response


def test_extracts_code_from_yaml_response():
    response = """```yaml
action: generate_code
code: |
  print('hello')
```"""

    assert extract_code_from_response(response) == "print('hello')"


def test_extracts_python_code_fence():
    response = "Run this:\n```python\nprint('hello')\n```"

    assert extract_code_from_response(response) == "print('hello')"


def test_preserves_empty_python_code_fence():
    assert extract_code_from_response('```python\n```') == ''


def test_returns_none_when_response_has_no_code():
    assert extract_code_from_response('No code was generated.') is None