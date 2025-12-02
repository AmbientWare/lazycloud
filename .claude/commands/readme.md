You are a technical documentation expert tasked with updating the root README.md file to reflect recent changes in the codebase.

# Task

Update the root README.md file to accurately reflect any relevant changes that have been made since the main branch.

# Process

Follow these steps in order:

1. **Read the current README.md**
   - Examine the file structure, sections, and content
   - Note the documentation style, tone, and level of detail
   - Identify which sections cover what aspects of the project

2. **Analyze git changes**
   - Run `git diff main...HEAD` to see all commits on the current branch
   - Run `git status` to see any uncommitted/staged changes
   - Run `git log --oneline main..HEAD` to see commit history since main

3. **Identify relevant changes**
   - Focus on changes that affect:
     - Project structure (new/moved/deleted directories or apps)
     - Developer workflow (setup, development, testing, deployment)
     - Architecture or key technologies
     - CLI commands or API endpoints
     - Configuration or environment variables
     - Dependencies or prerequisites
   - Ignore changes that are:
     - Internal implementation details
     - Minor refactors that don't affect external behavior
     - Temporary or experimental code

4. **Plan updates**
   - List which README sections need updates
   - For each section, describe what specific changes are needed
   - Consider whether new sections should be added or existing ones reorganized

5. **Apply updates**
   - Make edits that match the existing writing style and formatting
   - Keep descriptions concise but informative
   - Update code examples if commands or APIs have changed
   - Ensure consistency with the rest of the document

# Guidelines

- **Preserve style**: Match the existing tone, voice, and formatting conventions
- **Be selective**: Only document changes that a developer would need to know about
- **Stay current**: Remove outdated information that no longer applies
- **Be accurate**: Test commands and verify information before documenting
- **Think user-first**: Consider what someone cloning the repo would need to know

# Output

After completing the analysis and updates, provide:
1. A summary of what changed in the codebase
2. A list of which README sections you updated and why
3. The actual edits made to README.md 