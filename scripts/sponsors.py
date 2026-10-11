"""Credits baney75's public GitHub sponsors in the calling repository.

Daily: rewrites the block between the sponsors:start and sponsors:end comment lines in README.md.
On a published release: appends a thank-you naming current sponsors to the release notes.
Only GitHub logins are written, and each is checked against GitHub's login format, so a sponsor's
display name can never inject Markdown or HTML into a README or release.
"""
import json, os, re, subprocess, sys

OWNER = 'baney75'
LOGIN = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$')
# The markers count only when each sits alone on its own line, so a README that mentions them
# in prose or a code span is never rewritten.
BLOCK = re.compile(r'^<!-- sponsors:start -->[ \t]*\r?\n(.*?)^<!-- sponsors:end -->[ \t]*\r?$', re.M | re.S)
SPONSOR_URL = f'https://github.com/sponsors/{OWNER}'


def gh(*args, data=None):
    try:
        return subprocess.run(['gh', *args], input=data, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as error:
        sys.stderr.write(error.stderr)
        raise


def sponsors():
    query = '{user(login:"%s"){sponsors(first:100){nodes{... on User{login} ... on Organization{login}}}}}' % OWNER
    response = json.loads(gh('api', 'graphql', '-f', f'query={query}'))
    user = (response.get('data') or {}).get('user')
    if response.get('errors') or not user:
        sys.exit(f'GitHub did not return the sponsor list: {response.get("errors")}')
    return sorted({n['login'] for n in user['sponsors']['nodes'] if n and LOGIN.match(n.get('login', ''))}, key=str.lower)


def credit(names):
    return ' · '.join(f'[@{n}](https://github.com/{n})' for n in names)


def readme(names):
    path = 'README.md'
    if not os.path.exists(path):
        return print('No README.md; nothing to update.')
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    match = BLOCK.search(text)
    if not match:
        return print('README.md has no sponsors block; nothing to update.')
    if not names and '](https://github.com/' in match.group(1):
        # An empty list while sponsors are already credited more likely means the token could not
        # see them than that every sponsor left. Fail loudly instead of erasing the credits.
        sys.exit('GitHub returned no sponsors but README.md credits some; leaving it unchanged.')
    nl = '\r\n' if '\r\n' in text else '\n'
    body = (f'Thank you to everyone who sponsors this work: {credit(names)}.' if names else
            f'No sponsors yet. [Sponsor on GitHub]({SPONSOR_URL}) and your name appears here automatically.')
    updated = text[:match.start(1)] + body + nl + text[match.end(1):]
    if updated == text:
        return print('Sponsors block is current.')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(updated)
    print(f'Updated README.md with {len(names)} sponsor(s).')


def release(names, release_id, repo):
    if not names:
        return print('No sponsors to thank in this release.')
    notes = json.loads(gh('api', f'repos/{repo}/releases/{release_id}')).get('body') or ''
    if '### Thanks to sponsors' in notes:
        return print('Release already thanks sponsors.')
    notes = f'{notes.rstrip()}\n\n### Thanks to sponsors\n\n{credit(names)}\n'
    gh('api', '-X', 'PATCH', f'repos/{repo}/releases/{release_id}', '--input', '-', data=json.dumps({'body': notes}))
    print(f'Thanked {len(names)} sponsor(s) in release {release_id}.')


if __name__ == '__main__':
    names = sponsors()
    if os.environ.get('EVENT') == 'release' and os.environ.get('RELEASE_ID'):
        release(names, os.environ['RELEASE_ID'], os.environ['GITHUB_REPOSITORY'])
    else:
        readme(names)
