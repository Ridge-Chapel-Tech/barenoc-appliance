# BareNOC — Passkey Login Guide

**Audience:** customers and their local IT person
**Time to read:** 2 minutes

> BareNOC uses **passkeys** instead of passwords. A passkey is a secure credential
> stored on your device (biometric, security key, or phone) — it never leaves your
> device, and there's no password to remember or steal.

---

## For Admins: Inviting a User

1. Sign in to BareNOC with your passkey.
2. Go to **Settings → Identity & Passkeys** and click **Open Pocket ID admin**.
3. In Pocket ID: **Users → Invite user** → enter the user's email.
4. (Recommended) Assign the group **`barenoc-operators`** so they can act on tickets.
5. Send. The user receives an email with their invite link.

*Admins get the `barenoc-admins` group; that group grants the admin role in BareNOC.*

---

## For Users: Enrolling Your Passkey (2 minutes)

1. Open the **invite email** and click the link (or ask your admin for it).
2. Enter your name.
3. Click **Create passkey** and confirm on your device:
   - **Phone/tablet:** use Face ID / fingerprint / your device's screen lock.
   - **Laptop:** use Windows Hello / Touch ID, or plug in a security key (YubiKey).
   - **Security key:** press the button on the key.
4. Done — you're enrolled.

---

## Signing In (every time, ~10 seconds)

1. Open BareNOC in your browser.
2. Click **"Sign in with passkey"**.
3. Confirm on your device.
4. You're in.

---

## Recovery Codes — READ THIS

**When you enroll your passkey, save the 10 one-time recovery codes.** Store them
in a password manager or print them and keep them somewhere safe.

- **Lost your device?** Use a recovery code to get back in, then enroll a new passkey.
- **Used all your codes?** Ask an admin to reset your passkeys from Pocket ID admin
  (Users → your account → reset credentials), then re-enroll.

**Never share your recovery codes.** Anyone with them can get into your account.

---

## Locked Out?

| Situation | What to do |
|-----------|-----------|
| Lost device, have recovery codes | Use a code to sign in, then enroll a new passkey |
| Lost device, no codes | Ask an admin: Pocket ID admin → Users → reset your passkeys |
| The BareNOC admin is locked out | Contact support — the appliance has a vendor break-glass passkey + a local admin fallback |

---

## Troubleshooting

- **"Sign in with passkey" button missing?** Passkey login isn't enabled on this
  appliance yet — ask your admin to enable it in Settings → Identity & Passkeys.
- **Passkey works on one device but not another:** passkeys are device-bound (or
  synced by your phone's account, e.g., iCloud/Google). Enroll once per device, or
  use your phone's passkey sync.
- **Browser not prompting:** use the latest Chrome, Edge, Firefox, or Safari; a
  security key must be plugged in before clicking the button.
