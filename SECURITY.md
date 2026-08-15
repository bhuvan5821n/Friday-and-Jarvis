# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public GitHub issue for security vulnerabilities
2. **Do NOT** include API keys, passwords, or credentials in any report
3. Use GitHub's private vulnerability reporting feature if available, or
   contact the maintainer through a private channel

## API Key Security

### Protecting Your Credentials

- **Never commit API keys** to Git repositories
- **Never share API keys** in public forums, issues, or chat
- Store API keys in `.env` file (already in `.gitignore`)
- Use environment variables when possible

### If Your API Key is Exposed

1. **Immediately rotate** your Gemini API key at https://aistudio.google.com/apikey
2. Delete the old key
3. Generate a new key
4. Update your `.env` or `config/api_keys.json` with the new key
5. Review your Google Cloud activity for unauthorized usage

### What We Never Do

- We never ask for your API key
- We never store your API key on any server
- We never transmit your API key except to the intended AI provider
- We never include API keys in source code, logs, or error messages

## Local Data Security

### Files to Protect

The following files contain sensitive data and should **never** be shared:

- `.env` - Contains API keys
- `config/api_keys.json` - Contains API keys
- `memory/long_term.json` - Contains your personal memory
- `Studios/Chat/conversations.json` - Contains your conversation history

### Git Protection

The `.gitignore` file is configured to prevent accidental commits of:
- API keys and credentials
- Personal memory files
- Conversation history
- Log files
- Database files

Before committing, verify that no sensitive files are staged:
```bash
git status
git diff --cached
```

## Best Practices

1. **Use a dedicated API key** for FRIDAY/JARVIS, not your main Google account key
2. **Monitor your API usage** for unexpected activity
3. **Keep your installation updated** to receive security patches
4. **Review configuration** after updates to ensure settings are preserved
5. **Use strong passwords** for any accounts linked to your API keys

## Dependencies

Regularly update dependencies to receive security patches:
```bash
pip install --upgrade -r requirements.txt
npm update
```

Review dependency security:
```bash
pip audit
npm audit
```

## Disclaimer

This is open-source software provided "as is" without warranty. While we
strive to follow security best practices, users are responsible for:
- Securing their own API keys
- Protecting their local data
- Reviewing code before use
- Understanding the risks of AI-assisted automation
