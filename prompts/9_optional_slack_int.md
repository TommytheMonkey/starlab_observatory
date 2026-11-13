Implement optional Slack integration for Starlab Observatory.

OBJECTIVE

Users should be able to configure Slack through the web
interface without manually writing a Slack app manifest.

Slack must not be required for Starlab installation
or operation.

SETUP

Create:

Settings → Integrations → Slack

Provide:

- Enable Slack integration
- Generate Slack App Manifest
- Copy manifest
- Download manifest
- Setup instructions
- Credential fields
- Test connection
- Save configuration
- Disable integration

MANIFEST

Generate a valid Slack app manifest.

Include only required permissions for enabled features.

Prefer Socket Mode for installations without public
HTTPS endpoints.

CREDENTIALS

Support:

- Bot User OAuth Token
- Signing Secret
- App-Level Token when using Socket Mode

Store credentials securely.

Never expose credentials in logs.

Never include credentials in configuration exports.

FEATURES

Support optional:

- Upcoming pass notifications
- Recording start notifications
- Recording completion notifications
- Recording failure alerts
- Receiver offline alerts
- Storage warnings
- Decoded image notifications

SLASH COMMANDS

Implement configurable commands such as:

/starlab status
/starlab passes
/starlab receivers
/starlab recordings

Allow authorized recording commands where supported.

SECURITY

Verify Slack requests when applicable.

Restrict privileged commands to authorized users.

Implement appropriate rate limiting.

TESTING

Use mocked Slack API responses.

Verify Starlab works normally when Slack is disabled.

DELIVERABLE

Complete optional Slack integration with manifest
generation and secure credential configuration.