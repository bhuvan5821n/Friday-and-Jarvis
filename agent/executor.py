import json
import sys
import threading
from pathlib import Path
from typing import Callable

from agent.planner       import create_plan, replan
from agent.error_handler import analyze_error, generate_fix, ErrorDecision
from aoca.tools          import ToolNotRegistered, registry


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    if not step_results:
        return params

    params = dict(params)

    if tool == "file_controller" and params.get("action") in ("write", "create_file"):
        content = params.get("content", "")
        if not content or len(content) < 50:
            all_results = [
                v for v in step_results.values()
                if v and len(v) > 100 and v not in ("Done.", "Completed.")
            ]
            if all_results:
                combined = "\n\n---\n\n".join(all_results)
                translated = _translate_to_goal_language(combined, goal)
                params["content"] = translated
                print(f"[Executor] 💉 Injected + translated content")

    return params
def _detect_language(text: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel("gemini-2.5-flash-lite")
    try:
        response = model.generate_content(
            f"What language is this text written in? "
            f"Reply with ONLY the language name in English (e.g. Turkish, English, French).\n\n"
            f"Text: {text[:200]}"
        )
        return response.text.strip()
    except Exception:
        return "English"


def _translate_to_goal_language(content: str, goal: str) -> str:
    if not goal:
        return content
    try:
        import google.generativeai as genai
        genai.configure(api_key=_get_api_key())
        model = genai.GenerativeModel("gemini-2.5-flash")

        target_lang = _detect_language(goal)
        print(f"[Executor] 🌐 Translating to: {target_lang}")

        prompt = (
            f"You are a professional translator. "
            f"Translate the following text into {target_lang}.\n"
            f"IMPORTANT:\n"
            f"- Translate EVERYTHING, leave nothing in English\n"
            f"- Keep all facts, numbers, and data intact\n"
            f"- Keep the structure and formatting\n"
            f"- Output ONLY the translated text, nothing else\n\n"
            f"Text to translate:\n{content[:4000]}"
        )
        response = model.generate_content(prompt)
        translated = response.text.strip()
        print(f"[Executor] ✅ Translation done ({target_lang})")
        return translated
    except Exception as e:
        print(f"[Executor] ⚠️ Translation failed: {e}")
        return content

def _call_tool(tool: str, parameters: dict, speak: Callable | None) -> str:
    """Dispatch through the registry. An unregistered name raises.

    There is deliberately no fallback branch. The previous version answered an
    unknown tool name by asking a model to write Python and running it in a
    subprocess, which made a hallucinated name arbitrary code execution.
    `registry.require` raises `ToolNotRegistered` instead, and the suggestion it
    carries is text for the user — nothing here can act on it.
    """
    import time as _time

    from aoca.events import EventState, emit

    definition = registry.require(tool)
    definition.validate(parameters)
    started_at = _time.monotonic()
    emit("action.executing", EventState.EXECUTING, tool=tool)
    try:
        result = definition.handler(parameters=parameters, speak=speak)
    except Exception as exc:
        emit("action.failed", EventState.FAILED, tool=tool,
             duration_ms=int((_time.monotonic() - started_at) * 1000),
             error_code=type(exc).__name__)
        raise
    emit("action.executed", EventState.EXECUTED, tool=tool,
         execution_started=True,
         duration_ms=int((_time.monotonic() - started_at) * 1000))
    return result or definition.default_result

class AgentExecutor:

    MAX_REPLAN_ATTEMPTS = 2

    def execute(
        self,
        goal:        str,
        speak:       Callable | None        = None,
        cancel_flag: threading.Event | None = None,
    ) -> str:
        from aoca import trace as _trace

        print(f"\n[Executor] 🎯 Goal: {goal}")

        # The executor is entered from a worker thread, so it starts its own
        # trace rather than inheriting one that may not have crossed over.
        with _trace.trace(origin="local_ui", stage="agent"):
            return self._execute(goal, speak, cancel_flag)

    def _execute(
        self,
        goal:        str,
        speak:       Callable | None        = None,
        cancel_flag: threading.Event | None = None,
    ) -> str:
        replan_attempts = 0
        completed_steps = []
        step_results    = {} 
        plan            = create_plan(goal)

        while True:
            steps = plan.get("steps", [])

            if not steps:
                msg = "I couldn't create a valid plan for this task, sir."
                if speak: speak(msg)
                return msg

            success      = True
            failed_step  = None
            failed_error = ""

            for step in steps:
                if cancel_flag and cancel_flag.is_set():
                    if speak: speak("Task cancelled, sir.")
                    return "Task cancelled."

                step_num = step.get("step", "?")
                tool     = step.get("tool", "")
                desc     = step.get("description", "")
                params   = step.get("parameters", {})

                if not registry.contains(tool):
                    # No retry, no replan, no "fix". A step naming a tool that
                    # does not exist cannot be made to work by trying again, and
                    # the old default for a missing tool was code execution.
                    suggestion = registry.suggest(tool)
                    msg = (f"I don't have a way to do that step"
                           + (f" — did you mean {suggestion}?" if suggestion
                              else ".")
                           + " Nothing was run.")
                    print(f"[Executor] ⛔ Unregistered tool {tool!r} — refused")
                    if speak: speak(msg)
                    return msg

                params = _inject_context(params, tool, step_results, goal=goal)

                print(f"\n[Executor] ▶️ Step {step_num}: [{tool}] {desc}")

                attempt = 1
                step_ok = False

                while attempt <= 3:
                    if cancel_flag and cancel_flag.is_set():
                        break
                    try:
                        result = _call_tool(tool, params, speak)
                        step_results[step_num] = result 
                        completed_steps.append(step)
                        print(f"[Executor] ✅ Step {step_num} done: {str(result)[:100]}")
                        step_ok = True
                        break

                    except Exception as e:
                        error_msg = str(e)
                        print(f"[Executor] ❌ Step {step_num} attempt {attempt} failed: {error_msg}")

                        recovery = analyze_error(step, error_msg, attempt=attempt)
                        decision = recovery["decision"]
                        user_msg = recovery.get("user_message", "")

                        if speak and user_msg:
                            speak(user_msg)

                        if decision == ErrorDecision.RETRY:
                            attempt += 1
                            import time; time.sleep(2)
                            continue

                        elif decision == ErrorDecision.SKIP:
                            print(f"[Executor] ⏭️ Skipping step {step_num}")
                            completed_steps.append(step)
                            step_ok = True
                            break

                        elif decision == ErrorDecision.ABORT:
                            msg = f"Task aborted, sir. {recovery.get('reason', '')}"
                            if speak: speak(msg)
                            return msg

                        else:
                            fix_suggestion = recovery.get("fix_suggestion", "")
                            if fix_suggestion:
                                try:
                                    fixed_step = generate_fix(step, error_msg, fix_suggestion)
                                    # The fix comes from a model, so its tool
                                    # name is untrusted input. `_call_tool`
                                    # raises on an unregistered name; checking
                                    # first keeps that out of the retry path.
                                    if not registry.contains(fixed_step.get("tool")):
                                        raise ToolNotRegistered(
                                            str(fixed_step.get("tool")))
                                    if speak: speak("Trying an alternative approach, sir.")
                                    res = _call_tool(
                                        fixed_step["tool"],
                                        fixed_step["parameters"],
                                        speak
                                    )
                                    step_results[step_num] = res
                                    completed_steps.append(step)
                                    step_ok = True
                                    break
                                except Exception as fix_err:
                                    print(f"[Executor] ⚠️ Fix failed: {fix_err}")

                            failed_step  = step
                            failed_error = error_msg
                            success      = False
                            break

                if not step_ok and not failed_step:
                    failed_step  = step
                    failed_error = "Max retries exceeded"
                    success      = False

                if not success:
                    break

            if success:
                return self._summarize(goal, completed_steps, speak)

            if replan_attempts >= self.MAX_REPLAN_ATTEMPTS:
                msg = f"Task failed after {replan_attempts} replan attempts, sir."
                if speak: speak(msg)
                return msg

            if speak: speak("Adjusting my approach, sir.")

            replan_attempts += 1
            plan = replan(goal, completed_steps, failed_step, failed_error)

    def _summarize(self, goal: str, completed_steps: list, speak: Callable | None) -> str:
        fallback = f"All done, sir. Completed {len(completed_steps)} steps for: {goal[:60]}."
        try:
            import google.generativeai as genai
            genai.configure(api_key=_get_api_key())
            model     = genai.GenerativeModel(model_name="gemini-2.5-flash-lite")
            steps_str = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
            prompt    = (
                f'User goal: "{goal}"\n'
                f"Completed steps:\n{steps_str}\n\n"
                "Write a single natural sentence summarizing what was accomplished. "
                "Address the user as 'sir'. Be direct and positive."
            )
            response = model.generate_content(prompt)
            summary  = response.text.strip()
            if speak: speak(summary)
            return summary
        except Exception:
            if speak: speak(fallback)
            return fallback