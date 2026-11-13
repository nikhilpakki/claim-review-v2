# Deploying on the EC2 box

Moving from `python3 app.py` in a terminal to gunicorn under systemd. No
virtualenv: everything installs against the system `python3`.

Set this once per shell so the commands below can be pasted as they are:

```bash
APP_DIR=/home/ec2-user/nikhil-workspace/claim-review-v3-beta
APP_USER=ec2-user
cd "$APP_DIR"
```

These are the paths on the box as it stands, and they are what
`deploy/claim-review.service` already carries - so if the folder has not
moved, the unit installs with no editing.

## 1. Find out what is true now

Guessing any of these wrong loses data or makes the app unreachable, and all
four are one command each.

```bash
pgrep -af "python3 app.py"                      # the running dev server
python3 -c "from config import Config; print(Config.DATABASE_PATH)"   # from $APP_DIR
ls -ld "$APP_DIR" var reviews.db 2>/dev/null    # who owns the runtime files
command -v nginx && sudo ss -ltnp | grep -E ':80|:5000'
```

The second one matters most: that path is the live database, with every user
account, review and rule in it. If the service runs as a different user than
the one that has been running `python3 app.py`, it will not be able to write
to it.

## 1b. Check this folder is the code these files came from

The folder is named `claim-review-v3-beta`, and the work described here was
committed to the `claim-review-v2` repository. Before pulling anything, find
out what is in there:

```bash
git -C "$APP_DIR" remote -v && git -C "$APP_DIR" log --oneline -3
```

Three possible answers:

- **A checkout of this repository.** `git pull` in step 3 is the right move.
- **A checkout of a different repository (a real v3).** Do not pull - you
  would overwrite a different codebase. Deploy this one to its own folder
  instead, on a different port, and point the unit at it.
- **"not a git repository".** The folder was copied or unzipped onto the box.
  Skip step 3's `git pull` and upload the new files instead, keeping `.env`
  and `var/` exactly as they are:

  ```bash
  # from your laptop, in the checkout
  rsync -av --exclude '.git' --exclude '.env' --exclude 'var/' \
      --exclude '__pycache__' ./ "$APP_USER@<instance>:$APP_DIR/"
  ```

  `var/` holds the database and the Textract cache, `.env` holds the
  credentials; neither is in the repository, and both must survive the copy.

## 2. Back the database up

```bash
python3 -c "
import sqlite3, time
from config import Config
out = '/var/tmp/reviews-%s.db' % time.strftime('%Y%m%d-%H%M')
src = sqlite3.connect('file:%s?mode=ro' % Config.DATABASE_PATH, uri=True)
dst = sqlite3.connect(out)
src.backup(dst)
dst.close(); src.close()
print('backed up to', out)"
ls -lh /var/tmp/reviews-*.db
```

Run from `$APP_DIR` - it imports the app's own config, so it backs up whatever
the app actually uses rather than whatever is named `reviews.db`. A live
`.backup` rather than `cp`: the app may be mid-write, and with WAL a plain copy
can be missing the most recent reviews.

## 3. Update the checkout

`app.py`'s listen address is likely edited in place on this box. That edit is
what the pull will collide with, and it is no longer needed - the address is a
setting now.

```bash
git status --short          # anything listed here is a local edit
git checkout -- app.py      # only if the diff is just the host= line
git pull
```

Check the diff first if `git status` shows anything else.

## 4. Install the dependencies

```bash
sudo pip3 install -r requirements.txt
/usr/bin/python3 -m gunicorn --version
```

If pip refuses with "externally-managed-environment", add
`--break-system-packages`. If `--version` fails, gunicorn went to a different
python3 - `which python3` tells you which one, and `ExecStart` in step 6 needs
that path.

## 5. Fill in .env

```bash
grep -q FLASK_SECRET_KEY .env || python3 -c "import secrets; print('FLASK_SECRET_KEY=' + secrets.token_hex(32))" >> .env
grep -q CLAIM_REVIEW_DEBUG .env || echo 'CLAIM_REVIEW_DEBUG=0' >> .env
```

`FLASK_SECRET_KEY` is what stops a restart signing every reviewer out: without
it the signing key is random per process. Set it once and leave it - changing
it later signs everyone out exactly once.

systemd parses this file itself, and it is stricter than a shell: `KEY=value`
only, no `export`, no backticks, no `$VAR` references, no line continuations.
`.env.example` lists every setting.

### Which way in?

**With nginx** (recommended) reviewers use port 80, gunicorn stays on
loopback, and there is somewhere to terminate TLS later:

```bash
sudo cp deploy/nginx-claim-review.conf /etc/nginx/conf.d/claim-review.conf
sudo nginx -t && sudo systemctl enable --now nginx && sudo systemctl reload nginx
```

Then open **80** in the security group. `.env` needs nothing; the default
`CLAIM_REVIEW_BIND=127.0.0.1:5000` is already right.

**Without nginx**, gunicorn serves reviewers directly:

```bash
echo 'CLAIM_REVIEW_BIND=0.0.0.0:5000' >> .env
```

Then open **5000** in the security group. There is no TLS and no upload limit
on this route; the fetch page posts registration-id CSVs through it.

## 6. Install the service

`deploy/claim-review.service` is already filled in for the path above.
Confirm that is still where the code is, that `User`/`Group` match
`$APP_USER`, and that `ExecStart` names the python3 from step 4.

```bash
grep -E '^(User|Group|WorkingDirectory|EnvironmentFile|ExecStart)=' deploy/claim-review.service
sudo install -m 644 deploy/claim-review.service /etc/systemd/system/claim-review.service
sudo systemctl daemon-reload
```

If the checkout is somewhere else, rewrite the paths on the way in rather
than editing the copy in the repository:

```bash
sed "s#/home/ec2-user/nikhil-workspace/claim-review-v3-beta#$APP_DIR#g" \
    deploy/claim-review.service | sudo tee /etc/systemd/system/claim-review.service >/dev/null
```

Make sure the runtime files belong to the account the service runs as:

```bash
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
```

## 7. Stop the old server, start the new one

Port 5000 can only be held by one of them.

```bash
sudo pkill -f "python3 app.py"; sleep 2; pgrep -af "python3 app.py" || echo "dev server stopped"
sudo systemctl enable --now claim-review
systemctl status claim-review --no-pager
```

## 8. Check it

```bash
systemctl show -p Environment claim-review        # CLAIM_REVIEW_BIND as intended?
sudo ss -ltnp | grep -E ':80|:5000'               # listening where you expect
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/login
journalctl -u claim-review -n 50 --no-pager
```

Then from your own machine, against the instance's public address: sign in,
open a claim, and confirm the reviews that were there before still are. Those
reviews are the real test that step 1 found the right database.

## If it will not start

`journalctl -u claim-review -n 50` says which:

- `Failed to load environment files` - there is no `.env` in `$APP_DIR`.
  The unit will not start without it, deliberately: it holds the warehouse
  credentials, and starting without them fails later and less clearly.
- `203/EXEC` - `ExecStart` path is wrong; redo step 4.
- `Permission denied` on the database or `var/` - redo the `chown` in step 6.
- `Address already in use` - the old dev server is still up; redo step 7.
- Starts, but reviewers get nothing - nothing is listening where they are
  looking. Compare step 8's `ss` output against the security group.

## Rolling back

Nothing in this changes the database, so going back is just stopping the
service:

```bash
sudo systemctl disable --now claim-review
python3 app.py
```
