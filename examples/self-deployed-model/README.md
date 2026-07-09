# Self-Deployed Model

Self-deployed mode runs OpenEvo against a remote model-serving backend such as
vLLM. The user provides a Hugging Face model ID or compatible model
configuration in Desktop, and Core Backend owns the serving lifecycle.

This directory documents the release-facing self-deployed configuration shape.
The current backend API exposes the mode and service preparation boundary; a
fully automated runnable example should be added when model-serving lifecycle
control is owned end to end by Core Backend.
