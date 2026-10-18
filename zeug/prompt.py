"""Creative prompt builder for a Seedance draft.

Encodes the prompt rules in docs/AGENT-OPERATIONS.md plus the two lessons from the author's
walkthrough: the guidance video carries no scene colour, so describe the place (never the
people), and replace the whole subject rather than tracing the depth outline.
workflow.py appends its own audio and reference-token lines; this builds the part above them.
"""

GUIDANCE = {
    'depth': 'The colored depth silhouette in @Video1 is motion guidance only; no depth colors may remain in the output.',
    'depth_mesh': 'The colored depth silhouette and the face-mesh lines in @Video1 are motion and expression guidance only; neither may remain in the output.',
    'face_mesh': 'The face-mesh lines in @Video1 are expression and lip-motion guidance only and must not appear in the output.',
}


def parse_identity(spec):
    """'@Image1=woman, 30s, short black bob' -> ('@Image1', 'woman, 30s, short black bob')."""
    token, sep, description = spec.partition('=')
    token, description = token.strip(), description.strip()
    if not sep or not token.startswith('@') or not description:
        raise ValueError(f'identity must look like "@Image1=description", got {spec!r}')
    return token, description


def build(identities, scene, mode='depth', dialogue=None, positions=None):
    """identities: [(token, description)] in subject order; positions: optional ['left', 'right']."""
    if not identities:
        raise ValueError('at least one identity is required')
    if not scene or not scene.strip():
        raise ValueError('describe the scene: without it the draft comes back without the room\'s color')
    if mode not in GUIDANCE:
        raise ValueError(f'mode must be one of {", ".join(GUIDANCE)}')
    n = len(identities)
    lines = [f'Replace the {"only subject" if n == 1 else f"{n} subjects"} in @Video1 in every frame.']
    for i, (token, description) in enumerate(identities):
        where = f'The person on the {positions[i]}' if positions and i < len(positions) else (
            'The subject' if n == 1 else f'Subject {i + 1}')
        lines.append(f'{where} becomes {token}: {description}.')
    lines += [
        'Replace each whole subject, including hair, hands and clothing outside the guidance silhouette; '
        'match the reference build and hair length rather than the original outline.',
        GUIDANCE[mode],
        f'[Scene] {scene.strip().rstrip(".")}.',
        'Keep the camera, crop, framing, background, lighting and timing of @Video1 exactly.',
    ]
    if dialogue and dialogue.strip():
        lines.append(f'Dialogue, verbatim and with the original timing: "{" ".join(dialogue.split())}"')
    lines.append('Use only the audio embedded in @Video1 for lip synchronization.')
    return '\n'.join(lines) + '\n'
