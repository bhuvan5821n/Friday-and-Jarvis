# Privacy Policy

## Overview

FRIDAY/JARVIS is a personal AI assistant that runs locally on your computer.
This document describes how your data is handled in the current public version.

## Data Storage

### Local Memory
- Your personal memory (preferences, notes, contacts) is stored locally in `memory/long_term.json`
- This data never leaves your computer
- You can clear your memory at any time through the assistant or by deleting the file

### API Credentials
- Your Gemini API key is stored locally in `.env` or `config/api_keys.json`
- Your API key is **never** included in the source code or transmitted anywhere except to Google's Gemini API
- The project does not collect, transmit, or store your API key on any external server

### Conversation History
- Chat conversations are stored locally in `Studios/Chat/conversations.json`
- This data never leaves your computer

## External Services

### Google Gemini API
- When you use FRIDAY/JARVIS, your prompts are sent to Google's Gemini API for processing
- This is subject to [Google's Privacy Policy](https://policies.google.com/privacy)
- Your API key is used solely for authentication with Google's services

### No Telemetry
- This version of FRIDAY/JARVIS does **not** send any telemetry, usage data, or behavior analytics to any server
- No data is collected for training or improvement purposes
- No background data collection occurs

## Creator Information

The original creator of FRIDAY/JARVIS is **Bhuvan** (Instagram: @bhuvan5821na).
This information is stored in `config/identity.json` and is used solely for
attribution purposes. It is not transmitted anywhere and is separate from your
personal data.

## Your Rights

- **Access**: You can view all your data in the local JSON files
- **Deletion**: You can delete your memory, conversations, or any data file
- **Portability**: Your data is stored in standard JSON format
- **No tracking**: We do not track your usage or behavior

## Future Versions

Future versions may introduce optional features that could involve external
services. Any such features will:
- Be clearly documented
- Require explicit opt-in
- Default to disabled
- Never include your API keys or personal data

## Contact

If you have questions about privacy, please open an issue on the GitHub
repository. Do not include sensitive information (API keys, passwords, etc.)
in issue reports.
