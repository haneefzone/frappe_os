"""SecretsService round-trip + ed25519 keygen (CLAUDE.md rule 6)."""

import asyncssh
import pytest
from cryptography.fernet import Fernet

from app.core.security import (
    SecretKeyError,
    SecretsService,
    generate_ed25519_keypair,
)


def test_encrypt_decrypt_roundtrip():
    svc = SecretsService(Fernet.generate_key())
    plaintext = "-----BEGIN OPENSSH PRIVATE KEY-----\nsupersecret\n"
    token = svc.encrypt(plaintext)

    # The stored token is opaque: no plaintext leaks into it.
    assert token != plaintext
    assert "supersecret" not in token
    assert svc.decrypt(token) == plaintext


def test_malformed_key_refuses_to_boot():
    with pytest.raises(SecretKeyError):
        SecretsService("this-is-not-a-valid-fernet-key")


def test_token_from_another_key_cannot_be_decrypted():
    writer = SecretsService(Fernet.generate_key())
    reader = SecretsService(Fernet.generate_key())
    token = writer.encrypt("password123")
    with pytest.raises(SecretKeyError):
        reader.decrypt(token)


def test_generate_ed25519_keypair_shapes():
    private_pem, public_line = generate_ed25519_keypair()
    assert public_line.startswith("ssh-ed25519 ")
    assert "OPENSSH PRIVATE KEY" in private_pem
    # The private half is a real, importable key.
    key = asyncssh.import_private_key(private_pem)
    assert key is not None
