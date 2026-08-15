# FRIDAY/JARVIS - Personal AI Assistant

A personal AI assistant with voice control, screen awareness, and extensive automation capabilities.

**Original Creator:** Bhuvan  
**Instagram:** [@bhuvan5821na](https://www.instagram.com/bhuvan5821na/)

## Features

- **Voice Control**: Wake word detection ("Hey Jarvis") with real-time conversation
- **Dual Personas**: JARVIS (male) and FRIDAY (female) with distinct personalities
- **Screen Awareness**: Analyze what's on your screen or webcam
- **Browser Automation**: Control Chrome, Edge, Firefox, and other browsers
- **App Control**: Open, close, and manage applications
- **File Management**: Create, read, edit, organize, and search files
- **Smart Home Control**: Volume, brightness, WiFi, dark mode, and more
- **Web Search**: Search the web and summarize results
- **News Updates**: Get today's headlines and summaries
- **Weather Reports**: Current weather for any city
- **Flight Search**: Find flight options on Google Flights
- **YouTube Control**: Play, summarize, and search videos
- **Instagram Assistant**: Open Instagram, draft and send messages
- **Game Updates**: Manage Steam and Epic Games installations
- **Code Assistant**: Write, edit, explain, and run code
- **Reminders**: Set timed reminders using Task Scheduler
- **Long-term Memory**: Remember your preferences, contacts, and important facts
- **Battle Mode**: Serious mode with different visual theme and AI routing
- **Remote Control**: Control via WhatsApp (optional)

## Requirements

- Python 3.10+
- Windows 10/11 (primary support)
- Gemini API key (free tier available)
- Microphone and speakers (for voice control)
- Internet connection

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/friday-jarvis.git
   cd friday-jarvis
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Install Playwright browsers:**
   ```bash
   playwright install chromium
   ```

## First Launch

1. **Run the assistant:**
   ```bash
   python main.py
   ```

2. **First-run setup:**
   - FRIDAY will prompt you to configure your Gemini API key
   - Get a free key at: https://aistudio.google.com/apikey
   - Enter your key when prompted
   - The connection will be tested automatically

3. **Grant permissions:**
   - Allow microphone access for voice control
   - Allow screen capture for screen analysis (optional)

## Configuration

### API Keys

Your Gemini API key is stored in `.env` file:
```
GEMINI_API_KEY=your_key_here
```

You can also set it as an environment variable or in `config/api_keys.json`.

### User Profile

Configure your personal settings in `config/api_keys.json`:
```json
{
    "persona": "jarvis",
    "camera_index": 0,
    "chrome_profile_email": "your-email@gmail.com",
    "instagram_username": "your-instagram"
}
```

### Remote Control (Optional)

To enable WhatsApp remote control, add your number to `config/remote_control.json`:
```json
{
    "authorized_whatsapp_numbers": ["your_country_code_your_number"]
}
```

## How Memory Works

FRIDAY/JARVIS remembers important facts about you automatically:

- **Identity**: Name, age, city, job
- **Preferences**: Favorite foods, music, apps
- **Projects**: Things you're working on
- **Relationships**: Friends, family, colleagues
- **Notes**: Habits, routines, important dates

Memory is stored locally and never leaves your computer.

### Clearing Memory

To reset your memory:
- Ask FRIDAY: "Clear all memories"
- Or delete `memory/long_term.json`

**Note:** Creator identity (Bhuvan) is stored separately and cannot be cleared through normal memory operations.

## Voice Commands

### Wake Word
Say **"Hey Jarvis"** to activate the assistant (if wake word detection is enabled).

### Example Commands

- "What's the weather in London?"
- "Open Chrome"
- "Search the web for Python tutorials"
- "What's on my screen?"
- "Set a reminder for 3 PM tomorrow"
- "Play lo-fi music on YouTube"
- "What time is it?"
- "Switch to FRIDAY"
- "Enable battle mode"
- "Who created you?"

### Personas

- **JARVIS**: Professional, calm, Tony Stark's AI assistant style
- **FRIDAY**: Warm, caring, natural female voice with personality

Switch with: "Switch to FRIDAY" or "Go back to JARVIS"

## Development

### Project Structure

```
friday-jarvis/
├── main.py              # Main entry point
├── ui.py               # PyQt6 user interface
├── core/               # Core functionality
│   ├── ai.py           # AI model routing
│   ├── creator_identity.py  # Creator attribution
│   ├── prompt.txt      # System prompt
│   └── setup_wizard.py # First-run setup
├── actions/            # Tool implementations
├── memory/             # Memory management
├── Studios/            # Chat studio
├── remote_control/     # WhatsApp integration
├── config/             # Configuration files
├── tests/              # Unit tests
└── friday/             # FRIDAY persona UI
```

### Running Tests

```bash
python -m pytest tests/
```

### Code Style

- Python 3.10+ with type hints
- Async/await for concurrent operations
- PyQt6 for GUI
- Follow existing patterns when adding features

## Privacy & Security

See [PRIVACY.md](PRIVACY.md) for data handling details.  
See [SECURITY.md](SECURITY.md) for security best practices.

### Key Privacy Points

- Your API key stays on your computer
- Memory is stored locally
- No telemetry or tracking
- You can clear all personal data at any time
- Creator identity is separate from your personal data

## Troubleshooting

### "No module named 'sounddevice'"
```bash
pip install sounddevice
```

### "Gemini API key not found"
Run the setup wizard:
```bash
python core/setup_wizard.py
```

### "Microphone not working"
1. Check Windows microphone permissions
2. Verify microphone is selected in system settings
3. Try restarting the application

### "Browser automation not working"
```bash
playwright install chromium
```

## Current Limitations

- Primary support for Windows (macOS/Linux may work with adjustments)
- Voice control requires microphone access
- Some features require specific browsers installed
- Instagram automation requires Chrome with logged-in session
- Remote control requires WhatsApp setup

## License

A repository license has not yet been selected.

## Acknowledgments

- **Bhuvan** - Original creator and lead developer
- Google Gemini API - AI capabilities
- PyQt6 - User interface framework
- Playwright - Browser automation

---

**Original Creator:** Bhuvan  
**Instagram:** [@bhuvan5821na](https://www.instagram.com/bhuvan5821na/)
