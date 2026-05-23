# AGENTS.md

## Project

This project is GraphThinkingAgent (GTA), an experimental cognitive architecture for helping language-model agents manage complex engineering work. It focuses on task graphs, short-term and long-term memory, structured memory promotion, and LLM-friendly knowledge infrastructure.

## Collaboration Memory

Local collaboration memory for discussions about building this project lives in:

`.gta-local-memory/`

This directory is intentionally ignored by git. It is for private working notes, prompts, and memory about how we are developing GTA itself.

Do not confuse this folder with the memory management system that GTA will eventually build for agents. The ignored folder is only for our local project-development context; the product's future memory schemas, prompts, databases, RAG indices, ontology files, and runtime memory infrastructure should be designed separately in tracked project files when needed.

## Rules

1. Use python at `/Users/duanyufi/anaconda3/bin/python`.
2. For experiments that require API calls, use this fallback order:
   - `xiaomi` / `MiMo-V2.5-Pro`
   - `deepseek` / `deepseek-v4-pro`
   - `huiyan_cn` / `gpt-5.5`
   If all three fail, stop testing instead of trying additional models.
