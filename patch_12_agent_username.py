path = "app/services/agent/agent.py"
with open(path) as f:
    content = f.read()

warnings = []

# 1. Add user_name to process_message signature
old_sig = "    async def process_message(\n        self,\n        user_id: UUID,\n        user_message: str,\n        db: AsyncSession,"
new_sig = "    async def process_message(\n        self,\n        user_id: UUID,\n        user_message: str,\n        db: AsyncSession,\n        user_name: str = \"\","

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
else:
    warnings.append("process_message signature anchor not found")

# 2. Add user_name to _generate_response signature
old_gen_sig = "    async def _generate_response(\n        self,\n        history: list[dict],\n        context_note: str,\n        user_message: str,"
new_gen_sig = "    async def _generate_response(\n        self,\n        history: list[dict],\n        context_note: str,\n        user_message: str,\n        user_name: str = \"\","

if old_gen_sig in content:
    content = content.replace(old_gen_sig, new_gen_sig, 1)
else:
    warnings.append("_generate_response signature anchor not found")

# 3. Pass user_name to build_system_prompt in _generate_response
old_build = "        system_prompt = build_system_prompt(context_note=context_note)"
new_build = "        system_prompt = build_system_prompt(context_note=context_note, user_name=user_name)"

if old_build in content:
    content = content.replace(old_build, new_build, 1)
else:
    warnings.append("build_system_prompt call anchor not found")

# 4. Pass user_name in all handler calls from process_message
# The handlers are called via handlers dict in process_message
# We need to thread user_name through to _generate_response calls.
# Simplest: add user_name as kwarg to all _generate_response calls.
# Since there are many calls and they all use the same pattern,
# we'll update them all by replacing the pattern.
import re

# Replace all _generate_response calls that don't already have user_name
# Pattern: _generate_response(\n...user_message=user_message,\n        )
# We'll add user_name=user_name before the closing paren of each call
old_pattern = "            user_message=user_message,\n            )\n            return AgentResponse"
new_pattern = "            user_message=user_message,\n            )\n            return AgentResponse"
# Actually let's use a more targeted replacement

# Count occurrences of _generate_response without user_name
count_before = content.count("await self._generate_response(")
print(f"Found {count_before} _generate_response calls")

# Replace all by adding user_name parameter after user_message=user_message
# The pattern ends with: user_message=user_message,\n        )
content = content.replace(
    "            user_message=user_message,\n            )\n",
    "            user_message=user_message,\n            user_name=user_name,\n            )\n"
)

count_after = content.count("user_name=user_name,")
print(f"Added user_name= to {count_after} _generate_response calls")

with open(path, "w") as f:
    f.write(content)

if warnings:
    print("WARNINGS:")
    for w in warnings:
        print(f"  - {w}")
else:
    print("Done - agent.py: user_name flows through process_message -> handlers -> _generate_response -> build_system_prompt")
