
import requests

ip = 'localhost'

solution_str = """<think> I am omniscient. </think> The answer is \\boxed{24}."""
ground_truth = ['24']
data_source = 'deepscaler'

res = requests.post(f"http://{ip}:8388/compute_score", json={"solution_str": solution_str, "ground_truth": ground_truth, "data_source": data_source})
score = res.json()
res = score

print(score)