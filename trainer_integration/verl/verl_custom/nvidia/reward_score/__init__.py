# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# from . import gsm8k, math, prime_math, prime_code

def _default_compute_score(data_source, solution_str, ground_truth, extra_info=None):
    if data_source == 'deepscaler':
        from . import deepscaler
        res = deepscaler.deepscaler_reward_fn(solution_str, ground_truth)
    elif data_source[:9] == 'deepcoder':
        # data_source will be named as deepcoder_<dataset_name>, e.g. deepcoder_codeforces
        # this is to avoid collision with prime_code
        from . import deepcoder
        res = deepcoder.deepcoder_reward_fn(data_source[10:], solution_str, ground_truth)
    elif data_source == 'openai/gsm8k':
        from . import gsm8k
        res = gsm8k.compute_score(solution_str, ground_truth)
    elif data_source in ['lighteval/MATH', 'DigitalLearningGmbH/MATH-lighteval']:
        from . import math
        res = math.compute_score(solution_str, ground_truth)
    elif data_source in [
            'numina_aops_forum', 'numina_synthetic_math', 'numina_amc_aime', 'numina_synthetic_amc', 'numina_cn_k12',
            'numina_olympiads'
    ]:
        from . import prime_math
        res = prime_math.compute_score(solution_str, ground_truth)
    elif data_source in ['codecontests', 'apps', 'codeforces', 'taco', 'rStar']:
        from . import prime_code
        res = prime_code.compute_score(solution_str, ground_truth, continuous=True)
    elif data_source in ['tool_call']:
        from . import toolcall
        res = toolcall.compute_score(solution_str, ground_truth) 
    elif data_source == 'if_eval':
        from . import ifeval
        import json
        info = json.loads(ground_truth)
        prompt = info["prompt"]
        instruction_id_list = info["instruction_id_list"]
        kwargs = info["kwargs"]
        res = ifeval.compute_score_strict(prompt, solution_str, instruction_id_list, kwargs)
    elif data_source == 'llm_judge':
        from . import llm_judge
        import os
        import random
        
        # Extract information from ground_truth_info
        question = ground_truth.get('prompt')
        metadata = {
            "reference_answer": ground_truth.get('answer'),
            "extract_box": ground_truth.get('extract_box', False)
        }
        
        # Get base_url from environment variable, support list format
        base_url_env = os.getenv("OPENAI_BASE_URL", "https://integrate.api.nvidia.com/v1")
        # Parse as comma-separated list and randomly select one
        base_url_list = [url.strip() for url in base_url_env.split(',') if url.strip()]
        base_url = random.choice(base_url_list) if len(base_url_list) > 1 else base_url_list[0]
            
        sampling_params = {
            "base_url": base_url,
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "model": os.getenv("OPENAI_MODEL", "meta/llama-3.3-70b-instruct"),
            "temperature": float(os.getenv("OPENAI_TEMPERATURE", "0.0")),
            "top_p": float(os.getenv("OPENAI_TOP_P", "0.7")),
            "top_k": int(os.getenv("OPENAI_TOP_K", "-1")),
            "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS", "1024")),
            "max_model_len": int(os.getenv("OPENAI_MAX_MODEL_LEN", "8192"))
        }
        
        res = llm_judge.compute_score(question, solution_str, metadata, sampling_params)
    else:    
        raise NotImplementedError

    if isinstance(res, (int, float, bool)):
        return float(res)
    else:
        return float(res[0])

# Currently assuming all remote rewards are hosted on same server
def _remote_compute_score(data_source, solution_str, ground_truth, extra_info=None, session=None):
    import requests
    session = session or requests
    ip = extra_info['server_ip']
    if data_source == 'reasoning_gym':
        try:
            res = session.post(f"http://{ip}:8288/score", json={"answer": solution_str, "entry": extra_info['reward_model']['entry'], "task": extra_info['reward_model']['reasoning_task']})
            score = res.json()['score']
            res = score
        except Exception as e:
            print(f"Error: {e}, ip: {ip}")
            print(f"answer: {solution_str[:10]}, task: {extra_info['reward_model']['reasoning_task']}, entry: {extra_info['reward_model']['entry']}"[:100])
            res = 0
    else:
        try: 
            res = session.post(f"http://{ip}:8388/compute_score", json={"solution_str": solution_str, "ground_truth": ground_truth, "data_source": data_source})
            score = res.json()['score']
            res = score
        except Exception as e:
            print(f"Error: {e}, ip: {ip}")
            print(f"answer: {solution_str[:10]}, data_source: {data_source}, ground_truth: {ground_truth}"[:100])
            res = 0

    if isinstance(res, (int, float, bool)):
        return float(res)
    else:
        return float(res[0])

async def _remote_compute_score_async(data_source, solution_str, ground_truth, extra_info=None, session=None):
    import aiohttp
    # please pass in session, spinning each session is very bad
    session = session or aiohttp.ClientSession()
    ip = extra_info['server_ip']
    if data_source == 'reasoning_gym':
        try:
            async with session.post(f"http://{ip}:8288/score", json={"answer": solution_str, "entry": extra_info['reward_model']['entry'], "task": extra_info['reward_model']['reasoning_task']}) as res:
                res = await res.json()
                res = res['score']
        except Exception as e:
            print(f"Error: {e}, ip: {ip}")
            print(f"answer: {solution_str[:10]}, task: {extra_info['reward_model']['reasoning_task']}, entry: {extra_info['reward_model']['entry']}"[:100])
            res = 0
    else:
        try: 
            async with session.post(f"http://{ip}:8388/compute_score", json={"solution_str": solution_str, "ground_truth": ground_truth, "data_source": data_source}) as res:
                res = await res.json()
                res = res['score']
        except Exception as e:
            print(f"Error: {e}, ip: {ip}")
            print(f"answer: {solution_str[:10]}, data_source: {data_source}, ground_truth: {ground_truth}"[:100])
            res = 0

    if isinstance(res, (int, float, bool)):
        return float(res)
    else:
        return float(res[0])

