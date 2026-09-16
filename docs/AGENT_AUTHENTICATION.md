# Authenticate an agent in Chat

When an agent reports a missing or rejected credential, Chat shows an
**Authentication required** message. Select **Provider settings** to open the
workspace's authentication options. Expand the section named after your agent.
The original error remains available in the expandable error details.

Credentials belong to the workspace. Signing in on your laptop does not, by
itself, sign in the agent running inside the workspace.

| Agent | Authentication path |
| --- | --- |
| Claude Code | Use **Connect Claude account** in Provider settings and follow the sign-in flow, or save an **Anthropic API key** there. |
| Codex | Select **Open workspace terminal** and run `codex login` for ChatGPT sign-in. For API-key authentication, use `codex login --with-api-key`, which reads the key from standard input. The OpenAI field in Provider settings is for voice transcription, not Codex sign-in. |
| Antigravity | Open the workspace terminal, run `agy`, and follow its sign-in prompts. |
| Ante CLI | Run `ante` in the workspace terminal to configure its provider. Save that provider's supported key in Provider settings, or follow the provider's sign-in options in Ante. Available options depend on the installed Ante version. |
| DeepSeek Harness | Save a **DeepSeek API key** in Provider settings. |
| OpenRouter | Save an **OpenRouter API key** in Provider settings. This agent uses OpenCode with OpenRouter. |
| DeepSeek | Save a **DeepSeek API key** in Provider settings. This agent uses OpenCode with DeepSeek. |
| OpenCode Zen | Save an **OpenCode Zen API key** in Provider settings. If its gateway was not enabled when the workspace started, ask the workspace administrator to enable it. |
| LibreFang | Configure the agent's provider in the workspace terminal and save its supported provider key in Provider settings. |
| Opensource GPU | This uses the workspace fallback endpoint. Ask the workspace administrator to configure authentication if that endpoint requires it. |

After setup, return to Chat, select **Use prompt again**, and send the message.
Claude's connection status refreshes when you return to the chat window.
Ante can also show **Agent setup required** if its configured provider is
unavailable and its local fallback fails; check its provider configuration as
well as credentials.

An existing conversation keeps the agent it was created with. Select **New**
to choose a different agent. Where supported, the model picker can change the
model within the same agent while the conversation is idle; the next message
uses that model. Previous errors retain the conversation's actual agent label.
