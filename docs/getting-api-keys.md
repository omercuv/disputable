# Getting the API keys

The demo, the tests and `make eval` need no keys. Running live on the Airwallex sandbox needs the Airwallex keys, and letting Claude read needs a Claude key. Keys go only in the `.env` file in the project folder. Never commit them, paste them into a chat, or show them in a video.

| Keys | Used for | Needed |
|---|---|---|
| Airwallex sandbox Client ID + API key | every call to the Airwallex sandbox | for any run on the sandbox |
| Claude API key | the part that reads emails and records | only to let Claude read |

## 1. Airwallex sandbox keys

1. Open the sign-up form: https://www.sandbox.airwallex.com/global/signup
2. Fill in your email (a personal email is fine, but use one you can read), legal first and last name, business headquarters country, and a password. **Don't choose China:** China accounts don't get payments set up automatically.
3. Tick the Terms box and click **Create sandbox account**.
4. Log in at https://www.sandbox.airwallex.com/app/ (the sandbox app, not the real one at airwallex.com/app).
5. Stay on this first business. Don't add another business, and don't click **Payments > Get started** or ask for new payment methods. One builder got a "final" payments rejection email while setting up a new business. If you ever must add a business, name it exactly `Sandbox Business`.
6. Click **Developer** in the left menu, then the **API keys** tab.
7. Click **New scoped key** and name it `disputable-dev`. Use a scoped key: Airwallex is phasing out admin keys.
8. Set the permissions:
   - Under **Global permissions**, tick **Write** on Upload Files.
   - Under **Account permissions**, select your account in **Accounts**.
   - Under **Account permissions**, tick **Write** on Simulations, Payment Acceptance, Payment Disputes and Upload Files.
   - Click **Next** and finish the remaining screens. Copy the **Client ID** and the API key straight away, because Airwallex shows the key only once. If you lose it, regenerate it. The old key then stops working.
9. Create a file named exactly `.env` in the project folder, with these two lines. In Notepad, set **Save as type** to **All files** so the name doesn't end in `.txt`.
   ```
   AWX_CLIENT_ID=your client id
   AWX_API_KEY=your api key
   ```
10. Test them:
    ```bash
    uv run python scripts/spike.py login
    ```
    ```bash
    uv run python scripts/spike.py payments
    ```
    `login` should end with `OK: token received`. `payments` should end with three payments `SUCCEEDED`. It pays in a hidden browser window, so you need Microsoft Edge or Google Chrome installed.

### If something goes wrong

- **Business stuck "In review":** check the name (step 5). If that's not it, email devhelp@airwallex.com.
- **`login` fails with 401:** the key is wrong or was regenerated. Copy both values again.
- **403 on later calls:** the key is missing a permission. Edit the key and add the permissions from step 8. The key itself doesn't change.
- **"This account is not enabled for native API access":** this is expected if you send a raw card number from your own server. Airwallex's sandbox settings page says this access needs PCI-DSS certification. The spike doesn't use it: it pays with the test card inside Airwallex's own card form (the Airwallex.js Card element), which worked on our account on 2026-10-30.
- **`payments` fails with `no browser found`:** install Microsoft Edge or Google Chrome, or run `uv run playwright install chromium`.

## 2. Claude API key

1. Sign in at https://platform.claude.com. Using the same email as your claude.ai account makes plan credits easier to link.
2. Make sure the account has credits. Claude Pro doesn't include API credits. Max plans can link monthly API credits from claude.ai billing settings. You can also buy a small amount under **Settings > Billing**.
3. Open https://platform.claude.com/settings/keys and click **Create key**. Pick the Default Workspace and an expiry. The expiry can't be changed later.
4. Copy the key. It starts with `sk-ant-` and is shown only once. Put it in `.env` as `ANTHROPIC_API_KEY=...`.

Sources (checked 2026-10-29): the hackathon builder guide (airwallexdev.com/guide); the Airwallex docs pages "Sandbox environment", "Sandbox settings" and "Manage API keys"; the Claude Console docs.
