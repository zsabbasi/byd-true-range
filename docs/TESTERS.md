# Testing the BYD true-range app: a guide for non-developers

Thank you for trying this. It is an **unofficial, experimental** tool: it reads your BYD car's data from BYD's servers, keeps a history BYD does not, and shows a more honest range, your trips, your charging and your battery. It **cannot control your car**; it only reads.

You do not need to install anything or run any commands. You need a phone, the link you were given, and a BYD account (see step 1).

## Before you start: use a second BYD account, not your main one

The app signs in to BYD *as you*. If your login ever leaked, someone could do whatever the BYD app can do, including controlling your car. So please do **not** use your main BYD login.

1. Create a second BYD account (a different email or phone number).
2. In the BYD app, share your car with that second account.
3. Use the second account to sign in here. (If you need help with the sharing step, ask the person who invited you.)

## Who can see what: please read this

This is a **shared test server** run by someone you know. To keep reading your car while you are not looking at the page, the server has to keep your BYD login, encrypted on its disk.

- Other testers **cannot** see your data. Each person's login and history are kept in separate folders.
- The person who runs the server **could** read your login and your car's data (where you drive and charge) if they wanted to, because they control the machine. Only use a server run by someone you trust.
- Your login goes to BYD to be checked, and is only accepted over a secure (https) connection.
- You can delete your login and all your data yourself, any time (see the end).

## Signing in

1. Open the link on your phone. It goes straight to the sign-in page.
2. Enter your **second** BYD account and its password.
3. Country is already set to **Pakistan**. If you are somewhere else, choose "Other country" and type the two-letter code.
4. Tick both boxes (the second account, and that you understand who can see your data), then tap **Sign in**. It can take up to 25 seconds.
5. If the server asks for an **invite code**, type the one you were given.
6. You will then be asked for your **battery size**. In Pakistan most Atto 3 cars are the **Standard Range (49.92 kWh)**, but check your own car's papers: the app cannot tell, and a wrong choice makes every range number wrong.

## Put it on your home screen (optional)

- **Android (Chrome):** menu (three dots), then **Install app** (or **Add to Home screen**).
- **iPhone (Safari):** the Share button, then **Add to Home Screen**. (This has not been tested yet; please tell us how it goes.)

## What to expect

- The first readings appear within a minute or two. Range figures start from BYD's own measured consumption and become **your own** after about 50 km of driving the app has watched. Early numbers are rough, and it shows a **band**, not one number: trust the band.
- The app reads your car every 30 seconds while you drive and about every 15 minutes while parked. Reading can wake the car's system a little, like opening the BYD app does.
- Never rely on it as your only range check. Keep a reserve.

## If something goes wrong

| What you see | What it means |
|---|---|
| "BYD rejected the sign-in" (code 3008) | Wrong email/phone or password, or the wrong country. Check them. **Do not keep retrying**: many wrong attempts can lock your BYD account. |
| "This server has reached its limit of users" | It is full. Ask the person who runs it. |
| "This server needs an invite code" | Ask the person who invited you. |
| "Sign in needs a secure (https) connection" | You opened an http:// address. Use the https:// link you were given. |
| "BYD rejected this sign-in" on the Account page later | You changed your BYD password, or BYD logged the session out. Sign in again on the Account page. |
| Numbers look wrong | Check the battery size on the Account page first. Then tell us (below). |

## Signing out and deleting your data

On the **Account** page (tap "Account" at the bottom of the page):

- **Sign out on this device:** signs this phone out. The server **keeps tracking your car** and your data stays.
- **Delete my data:** permanently deletes your login and all your history from the server and stops tracking. Tap twice to confirm. There is no undo.

## Telling us what you found

The most useful things: does sign-in work for your country and car model, do the numbers match what you see, did a charge or a drive show up correctly. **Never send your password, and do not post screenshots that show where you live or drive.**
