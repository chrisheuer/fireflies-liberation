# Getting and storing your Fireflies API key

## Where the key is

<https://app.fireflies.ai/integrations/custom/fireflies>

That page shows your personal API key. In the app it is reachable from
Settings, under the Developer / custom-integration area, but the direct
link above is faster and is the one this tool's error message prints.

The key authenticates as **you**. Anyone holding it can read every
transcript your account can read. Treat it like a password.

## Which plan you need

Fireflies' own limits page says every tier can call the API, and differ
only in rate:

| plan | documented limit |
|---|---|
| Free | 50 requests/day |
| Pro | 500 requests/day |
| Business / Enterprise | 60 requests/min |

Source: [docs.fireflies.ai/fundamentals/limits](https://docs.fireflies.ai/fundamentals/limits).

**Two caveats worth knowing before you plan a dump.**

Some third-party write-ups claim API access is Business-tier only. The
official limits page contradicts that, and it lists per-day quotas for
Free and Pro, which would be meaningless if those tiers had no access.
We could not resolve the contradiction from documentation alone, so:
**check yours empirically before trusting either** —

```sh
python3 fireflies_dump.py whoami
```

If that returns your user, you have access. If it returns an auth error,
you do not, and no amount of retrying will change it.

Second: **one transcript is one request.** The rate limit is what decides
how long a dump takes, and the arithmetic is unforgiving at the low tiers.

| plan | 1,000 transcripts takes |
|---|---|
| Free, 50/day | ~20 days |
| Pro, 500/day | ~2 days |
| Business, 60/min | ~20 minutes |

`dump` is resumable, so a multi-day Free-tier pull is viable — run it
daily until `verify` is clean. Use `--rpm` to stay under a per-minute
ceiling; it defaults to 60.

## Storing the key

Two supported ways. The tool looks for them in this order, and never
writes the key to a file, a log line, or the dump.

### macOS Keychain (recommended)

Copy the key to your clipboard from the page above, then:

```sh
security add-generic-password -a "$USER" -s FIREFLIES_API_KEY -w "$(pbpaste)"
```

`$(pbpaste)` rather than pasting the key literally is deliberate: your
shell records the command **before** expanding it, so the history keeps
the harmless `$(pbpaste)` text instead of your live key.

Nothing further is needed — the tool reads the Keychain automatically.
Use `--keychain-service NAME` if you store it under a different name.

To check or replace it:

```sh
security find-generic-password -a "$USER" -s FIREFLIES_API_KEY -w   # print
security delete-generic-password -a "$USER" -s FIREFLIES_API_KEY    # remove
```

### Environment variable

Works everywhere, including Linux:

```sh
export FIREFLIES_API_KEY="..."
```

If you put it in a shell profile, that file now contains a live
credential in plaintext — `chmod 600` it, and keep it out of any repo.

### Not supported, on purpose

There is no config-file option and no `--api-key` flag. A key on the
command line lands in your shell history and in the process list, where
any other process on the machine can read it.

## Revoking

Regenerate the key on the same page. The old value stops working
immediately. Do that if it has ever been pasted into a chat, a ticket, a
screenshot, or a repository.

## What the tool talks to

Only two hosts: `api.fireflies.ai/graphql` for data, and the signed
media host Fireflies hands back for audio and video downloads. The key
goes to the first as an `Authorization: Bearer` header, and is
explicitly stripped from media requests, since those URLs are
pre-signed and would otherwise carry your credential to a storage host.
