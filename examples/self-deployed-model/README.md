# Self-Deployed Model

Self-deployed mode runs OpenEvo against a remote model-serving backend such as
vLLM. In the target product, the user selects a validated Hugging Face model
profile in Desktop. OpenEvo Daemon then owns model download, serving,
readiness, and session execution on the remote GPU server.

Self-Deployed is unavailable in the current Preview. A future runnable example
must remain a Desktop workflow: users will not SSH to the server to install
OpenEvo, start vLLM, download model files, or operate the Daemon manually.
