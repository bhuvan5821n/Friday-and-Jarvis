# Hermes WhatsApp plugin (deployed copy)

These files are the version-controlled source of truth for the plugin that
Hermes actually loads from its plugins directory.

They live in a separate directory because Hermes discovers user plugins under
its own home (`get_hermes_home()/plugins`), which is outside this repository.
Nothing here is imported by JARVIS or FRIDAY — this is Hermes-side code, kept
here so it is reviewable, diffable, and recoverable.

## What it does

`GatedWhatsAppAdapter` subclasses Hermes' bundled `WhatsAppAdapter` and
overrides `_should_process_message`, which is the first statement of
`_build_message_event` and therefore the last point at which a message can be
dropped before a `MessageEvent` exists. Dropping there means no session, no
read receipt, no hook context, no LLM call, no memory write, no tool
execution, and no log line containing the text.

The gate logic itself is **not** here — it is in `remote_control/security/`,
inside this project, loaded by explicit path. Keeping it on this side of the
boundary means upgrading or reinstalling Hermes cannot silently remove it.

## Why zero Hermes edits

The plugin's `plugin.yaml` declares `name: whatsapp-platform`, the same key as
the bundled plugin. Hermes scans user plugins after bundled ones and later
sources win on key collision, so this adapter replaces the bundled one through
a supported extension point. `remote_control/tests/test_hermes_plugin_gate.py`
asserts that the Hermes source tree contains no references to this work.

## Deploying a change

Copy the plugin files to your Hermes plugins directory:

```bash
cp remote_control/hermes_plugin/* "<HERMES_HOME>/plugins/whatsapp/"
```

Then re-run the integration test with the Hermes interpreter, which is the only
one that has Hermes' dependencies.

## Activation

Requires `plugins.enabled: [whatsapp-platform]` in your Hermes `config.yaml`.
Removing that entry does **not** fall back to the ungated bundled adapter — the
user manifest has already deduped it away, so WhatsApp disappears entirely.
That is the intended fail-closed behaviour.

## Rollback

Remove the plugin directory and restore your Hermes config backup:

```bash
rm -r "<HERMES_HOME>/plugins/whatsapp"
cp "<HERMES_HOME>/config.yaml.bak" "<HERMES_HOME>/config.yaml"
```

Hermes returns to its stock, ungated behaviour.
