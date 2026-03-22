import re
from collections import Counter
import json
import os

def validate_result(result, answer):
    if len(result) == 0 or len(answer) == 0:
        return 1 if len(result) == len(answer) else 0

    try:
        counter1_full = Counter(
            (item["name"], json.dumps(item["arguments"], sort_keys=True)) 
            for item in result
        )
        counter2_full = Counter(
            (item["name"], json.dumps(item["arguments"], sort_keys=True)) 
            for item in answer
        )
    except TypeError:
        return 0

    return 1 if counter1_full == counter2_full else 0

def extract_solution(tool_call_str):
    pattern = os.environ.get('TOOL_CALL_PATTERN', r'<tool_call>([\s\S]*?)<\/tool_call>')
    matches = list(re.finditer(pattern, tool_call_str, flags=re.DOTALL))
    if not matches:
        return None, tool_call_str
    
    tool_calls = []
    for match in matches:
        content = match.group(1).strip()
        try:
            parsed_content = json.loads(content)
            tool_calls.append(parsed_content)
        except json.JSONDecodeError:
            print(f"Failed to parse JSON: {content}")
            return None, tool_call_str
    
    if len(tool_calls) == 1:
        return tool_calls[0], tool_call_str
    else:
        return tool_calls, tool_call_str


def validate_format(tool_call_list):
    for item in tool_call_list:
        if not isinstance(item, dict):
            return 0
    for item in tool_call_list:
        if "name" not in item.keys() or "arguments" not in item.keys():
            return 0
    return 1

def compute_score(solution_str, ground_truth):
    """
    Returns:
       1  if the parsed result from 'solution_str' fully matches the 'ground_truth'
       0  otherwise
    """
    # Extract the tool call result and the full output string
    result, output_string = extract_solution(solution_str)

    # Ensure the "thinking" markers are present
    # comment out for llama-nemotron-nano
    # if "</think>" not in output_string:
    #     return 0

    # Ensure we actually extracted something
    if result is None:
        return 0

    # If the result is a single dictionary, wrap it in a list
    if isinstance(result, dict):
        result = [result]

    # Validate the format of the extracted result
    if not validate_format(result):
        return 0

    # Compare the extracted result with the ground truth
    if validate_result(result, ground_truth) == 1:
        return 1.0
    else:
        return 0 # format reward

if __name__ == "__main__":
    solution_str9 = """ <think>
Okay, the user is asking for cat breeds native to China and Canada. Let me check the tools available. There's the 'origin' tool that fetches cat breeds by country. The parameter 'origin' has a default value of Egypt, but the user wants China and Canada. I need to call the tool twice, once for each country. Wait, can I do that? The tool might accept multiple countries, but the parameter is a string. Maybe I should pass both as separate arguments. But the tool's parameters are defined with a single 'origin' field. Hmm, maybe the API allows multiple countries separated by commas. Let me check the tool's description again. The description says "a specified country", so maybe it's for a single country. Therefore, I need to make two separate calls, one for China and one for Canada. That's the correct approach here.
</think>

<tool_call>
{"name": "origin", "arguments": {"origin": "China"}}
</tool_call>
<tool_call>
{"name": "origin", "arguments": {"origin": "Canada"}}
</tool_call>
"""
    ground_truth9 = [{'name': 'origin', 'arguments': {'origin': 'China'}}, {'name': 'origin', 'arguments': {'origin': 'Canada'}}]

    print(f"Example 9 compute_score: {compute_score(solution_str9, ground_truth9)}")