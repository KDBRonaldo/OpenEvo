from openai import OpenAI
import os

LLM_JUDGE_DEBUG = int(os.getenv("LLM_JUDGE_DEBUG", "1"))

# STRICTLY match
STRICTLY_JUDGE_PROMPT_TEMPLATE = """Given a problem, determine whether the final answer in the provided (incomplete) solution process matches the reference answer.

The reference answer may be a single option character (e.g., A, B, C, D), a numerical value, an expression, or a list of answers if multiple questions are involved.

**The reference answer may be in Chinese or another language, but your evaluation should be language-agnostic.**

---

Your task:

1. Carefully compare the **final output** of the solution process with the **reference answer**.

2. Provide a **brief explanation first**, including:
   - Whether the final answer is clearly stated.
   - Whether it matches the reference answer **exactly** (including form, number, symbol, or character).
   - Any mismatch, ambiguity, or incompleteness in the solution process.

3. After the explanation, output only **YES** or **NO**, strictly in the format: `\\boxed{{YES}}` or `\\boxed{{NO}}`.

4. Matching must be **strict**:
   - Any discrepancy in number, character, expression, or format = **NO**.
   - If the solution is ambiguous, missing, or incomplete = **NO**.
   - Only a clear and exact match = **YES**.

---

**Question:**

{question}

**Solution Process (Final Step Only):**

{response}

**Reference Answer:**

{reference}

**Output:**

(Explain first, then give your final answer below in the format `\\boxed{{YES}}` or `\\boxed{{NO}}`.) 
"""

# EQUIVALENT match
EQUIVALENT_JUDGE_PROMPT_TEMPLATE = """Given a problem, determine whether the final answer in the provided (incomplete) solution process is equivalent to the reference answer.

The reference answer may be a single option character (e.g., A, B, C, D), a numerical value, an expression, or a list of answers if multiple parts are involved.

**The reference answer may appear in Chinese or other languages. Your evaluation should remain language-agnostic.**

---

Your task:

1. Analyze the final output in the solution process and compare it with the reference answer.

2. If they are **logically or mathematically equivalent**, output **YES**.

3. If they are **not equivalent**, output **NO**.

4. If the final step of the solution is unclear, incomplete, or ambiguous, treat it as incorrect and output **NO**.

---

Your output must be either **'YES'** or **'NO'**, formatted strictly as: `\\boxed{{YES}}` or `\\boxed{{NO}}`.

---

**Question:**  
{question}

**Solution Process (Final Step Only):**  
{response}

**Reference Answer:**  
{reference}

**Output:**

(First, briefly explain your reasoning. Then give your final answer in the required format: `\\boxed{{YES}}` or `\\boxed{{NO}}`.)"""


LLM_JUDGE_PROMPT_TEMPLATE = os.getenv("LLM_JUDGE_PROMPT_TEMPLATE", "EQUIVALENT")

if LLM_JUDGE_PROMPT_TEMPLATE == "STRICT":
    LLM_JUDGE_PROMPT_TEMPLATE = STRICTLY_JUDGE_PROMPT_TEMPLATE
elif LLM_JUDGE_PROMPT_TEMPLATE == "EQUIVALENT":
    LLM_JUDGE_PROMPT_TEMPLATE = EQUIVALENT_JUDGE_PROMPT_TEMPLATE
else:
    print(f"Use env variable LLM_JUDGE_PROMPT_TEMPLATE: {LLM_JUDGE_PROMPT_TEMPLATE}")


def extract_answer_from_box(string):
    """Extract Answer String from \\boxed or \\fbox expression."""
    if not string or not string.strip():
        return None

    # Try to find \\boxed first
    idx = string.rfind("\\boxed")
    box_type = "\\boxed"
    
    if idx < 0:
        # If not found, try \\fbox
        idx = string.rfind("\\fbox")
        box_type = "\\fbox"
        if idx < 0:
            return None

    i = idx
    right_brace_idx = None
    num_left_braces_open = 0
    while i < len(string):
        if string[i] == "{":
            num_left_braces_open += 1
        if string[i] == "}":
            num_left_braces_open -= 1
            if num_left_braces_open == 0:
                right_brace_idx = i
                break
        i += 1

    if right_brace_idx is None:
        return None
    else:
        retval = string[idx : right_brace_idx + 1]

    if retval:
        left = box_type + "{"
        try:
            assert retval[: len(left)] == left
            assert retval[-1] == "}"
            return retval[len(left) : -1]
        except AssertionError:
            return None

    return None

def compute_score(
    question: str,
    response_to_judge: str,
    metadata: dict,
    sampling_params_dict: dict,
) -> float:
    """Judges a single response using the LLM.

    Args:
        question: The question string.
        response_to_judge: The assistant's response string.
        metadata: Metadata containing reference answer and criteria.
        sampling_params_dict: Dictionary containing OpenAI API parameters including base_url and api_key.

    Returns:
        Score (float)
    """
    reference = metadata.get("reference_answer", "")
    extract_box = metadata.get("extract_box", False)

    # Remove thinking part if using thinking models
    response_to_judge = response_to_judge.split("</think>")[-1].strip()
    response_to_judge = response_to_judge.split("<answer>")[-1].strip()

    response_to_judge = (
        extract_answer_from_box(response_to_judge)
        if extract_box
        else response_to_judge
    )
    response_to_judge = "None" if response_to_judge is None else response_to_judge
    # Prioritize metadata's judge_prompt_template, then default
    # Note that if you want to use a custom judge_prompt_template, you may need to change the verdict extraction logic accordingly
    current_judge_prompt = (
        metadata.get("judge_prompt_template", None) or LLM_JUDGE_PROMPT_TEMPLATE
    )

    prompt = current_judge_prompt.format(
        question=question,
        response=response_to_judge,
        reference=reference,
    )
    if LLM_JUDGE_DEBUG:
        print(f"[LLM_JUDGE] Prompt: {prompt}")
    
    # Initialize OpenAI client with custom URL and key from sampling_params_dict
    client = OpenAI(
        base_url=sampling_params_dict.get("base_url", "https://integrate.api.nvidia.com/v1"),
        api_key=sampling_params_dict.get("api_key", "$API_KEY_REQUIRED_IF_EXECUTING_OUTSIDE_NGC")
    )
    
    # Extract OpenAI parameters from sampling_params_dict
    model = sampling_params_dict.get("model", "meta/llama-3.3-70b-instruct")
    temperature = sampling_params_dict.get("temperature", 0.2)
    top_p = sampling_params_dict.get("top_p", 0.7)
    top_k = sampling_params_dict.get("top_k", -1)
    max_tokens = sampling_params_dict.get("max_tokens", 1024)
    max_model_len = sampling_params_dict.get("max_model_len", 8192)
    
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}, "truncate_prompt_tokens": max_model_len - max_tokens, "top_k": top_k}
        )
        
        generated_text = ""
        for chunk in completion:
            if chunk.choices[0].delta.content is not None:
                generated_text += chunk.choices[0].delta.content
        
        generated_text = generated_text.strip()
        
    except Exception as e:
        if LLM_JUDGE_DEBUG:
            print(f"[LLM_JUDGE] Error calling OpenAI API: {e}")
        return 0.0

    score = 0.0
    if generated_text:
        generated_text_lower = generated_text.lower()
        result = extract_answer_from_box(generated_text_lower)
        # to avoid the case that the answer is not in the box
        if result is None:
            has_yes = "yes" in generated_text_lower
        else:
            has_yes = "yes" in result

        if has_yes:
            score = 1.0
            if LLM_JUDGE_DEBUG:
                print(f"[LLM_JUDGE] Parsed 'yes'. Score: {score}. Output: '{generated_text}'")
        else:
            score = 0.0
            if LLM_JUDGE_DEBUG:
                print(f"[LLM_JUDGE] No 'yes' found. Score: {score}. Output: '{generated_text}'")
    else:
        if LLM_JUDGE_DEBUG:
            print(f"[LLM_JUDGE] No output received from LLM.")
            
    if LLM_JUDGE_DEBUG:
        import sys
        sys.stdout.flush()

    return score