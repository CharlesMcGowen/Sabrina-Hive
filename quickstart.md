# Sabrina Console Quickstart Guide (Beta)

Welcome to the Sabrina Console beta! This guide will walk you through setting up your environment and running a sample mission.  This version excludes Kraken functionality.

## Prerequisites

*   **Python 3.9+:** Ensure you have Python installed on your Windows machine.
*   **Provider API Key:** Obtain an API key from your preferred AI provider (e.g., OpenAI, Anthropic). Replace `YOUR_PROVIDER_API_KEY` in the `.env.example` file with this key.  Keep your API key secure!

## Installation & Setup

1.  **Clone the Repository:** Clone the Sabrina Console repository to your local machine.
2.  **Configure Environment Variables:** Copy the `.env.example` file and rename it to `.env`. Edit this file to configure your provider details (API Key and Model Route).
3.  **Build the Release Kit:** Run `python release/console-kit/scripts/build_github_release.py` to create a ZIP archive containing the console application, configuration files, and example missions. This will create a zip named `dist/sabrina-console-release-kit.zip`.
4.  **Extract Release Kit:** Extract the contents of the `dist/sabrina-console-release-kit.zip` file into your desired installation directory.

## Running Your First Mission

1.  **Launch Sabrina Console:** Navigate to the extracted directory and run the Sabrina Console executable.  You should be greeted with a first-run configuration screen.
2.  **First Run Configuration**: Follow the on-screen prompts to verify model route, which will test your setup.
3.  **Guided Mission:** The console will guide you through one read-only mission.  Review the output and familiarize yourself with the interface.

## Release Report & Troubleshooting

You can find more detailed information and troubleshooting tips in `release/console-kit/scripts/release_report.md`.

## Important Notes

*   **Kraken Excluded:** This beta release does *not* include Kraken functionality.
*   **Security:** Keep your API key confidential and secure.  Avoid committing it to version control.