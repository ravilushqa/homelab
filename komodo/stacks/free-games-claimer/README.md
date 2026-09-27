# Free Games Claimer

Komodo-managed deployment of `P-Adamiec/Free-Games-Claimer-Remaster` at
`https://fgc.ravil.space`.

## Initial login

1. Open `https://fgc.ravil.space`.
2. Authenticate with PocketID.
3. Use the noVNC browser to sign in to Epic, Prime Gaming, GOG, Steam, and Fab
   interactively.
4. Complete captchas, 2FA, e-mail codes, and account challenges locally in the
   noVNC session.

noVNC shows the browser only while an active claim or login run is in progress.
After a completed run, it may show the empty X desktop. Restart or deploy the
stack to immediately begin a new run.

No store credentials, passwords, TOTP material, recovery codes, or webhook URLs
belong in Git. Browser sessions and claim state remain only in the `fgc_data`
Docker volume mounted at `/fgc/data`.

For future automation credentials or notification webhooks, create Komodo
secrets/variables and wire them into the stack environment at deploy time. Do
not paste secrets into chat or commit them to this repository.

Fab licence/EULA acceptance is disabled on first deployment. The browser will
stop for manual confirmation instead of accepting third-party terms
automatically.
