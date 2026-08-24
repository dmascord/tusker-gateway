# First, let's check the current imports in endpoints.py
with open('tusker_gateway/endpoints.py', 'r') as f:
    lines = f.readlines()

# Find the import section
import_section_idx = None
for i, line in enumerate(lines):
    if 'from tusker_gateway.endpoints import' in line:
        import_section_idx = i
        break

if import_section_idx is not None:
    # Add images_handler to the import statement
    lines[import_section_idx] = 'from tusker_gateway.endpoints import chat_completions_handler, responses_handler, anthropic_messages_handler, images_handler\n'
    print(f"✅ Updated import at line {import_section_idx + 1}")
else:
    print("❌ Could not find import section to update")

# Write back
with open('tusker_gateway/endpoints.py', 'w') as f:
    f.writelines(lines)

print("✅ Import updated")
