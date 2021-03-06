# -*- coding: utf-8 -*-
# test_crypto.py
# Copyright (C) 2013 LEAP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
"""
Tests for cryptographic related stuff.
"""
import binascii
import base64
import hashlib
import json
import os

from io import BytesIO

import pytest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidTag

from leap.soledad.common.document import SoledadDocument
from test_soledad.util import BaseSoledadTest
from leap.soledad.client import _crypto

from twisted.trial import unittest
from twisted.internet import defer


snowden1 = (
    "You can't come up against "
    "the world's most powerful intelligence "
    "agencies and not accept the risk. "
    "If they want to get you, over time "
    "they will.")


class AESTest(unittest.TestCase):

    def test_chunked_encryption(self):
        key = 'A' * 32

        fd = BytesIO()
        aes = _crypto.AESWriter(key, _buffer=fd)
        iv = aes.iv

        data = snowden1
        block = 16

        for i in range(len(data) / block):
            chunk = data[i * block:(i + 1) * block]
            aes.write(chunk)
        aes.end()

        ciphertext_chunked = fd.getvalue()
        ciphertext, tag = _aes_encrypt(key, iv, data)

        assert ciphertext_chunked == ciphertext

    def test_decrypt(self):
        key = 'A' * 32
        iv = 'A' * 16

        data = snowden1
        block = 16

        ciphertext, tag = _aes_encrypt(key, iv, data)

        fd = BytesIO()
        aes = _crypto.AESWriter(key, iv, fd, tag=tag)

        for i in range(len(ciphertext) / block):
            chunk = ciphertext[i * block:(i + 1) * block]
            aes.write(chunk)
        aes.end()

        cleartext_chunked = fd.getvalue()
        assert cleartext_chunked == data


class BlobTestCase(unittest.TestCase):

    class doc_info:
        doc_id = 'D-deadbeef'
        rev = '397932e0c77f45fcb7c3732930e7e9b2:1'

    @defer.inlineCallbacks
    def test_blob_encryptor(self):

        inf = BytesIO(snowden1)

        blob = _crypto.BlobEncryptor(
            self.doc_info, inf,
            secret='A' * 96)

        encrypted = yield blob.encrypt()
        preamble, ciphertext = _crypto._split(encrypted.getvalue())
        ciphertext = ciphertext[:-16]

        assert len(preamble) == _crypto.PACMAN.size
        unpacked_data = _crypto.PACMAN.unpack(preamble)
        magic, sch, meth, ts, iv, doc_id, rev = unpacked_data
        assert magic == _crypto.BLOB_SIGNATURE_MAGIC
        assert sch == 1
        assert meth == _crypto.ENC_METHOD.aes_256_gcm
        assert iv == blob.iv
        assert doc_id == 'D-deadbeef'
        assert rev == self.doc_info.rev

        aes_key = _crypto._get_sym_key_for_doc(
            self.doc_info.doc_id, 'A' * 96)
        assert ciphertext == _aes_encrypt(aes_key, blob.iv, snowden1)[0]

        decrypted = _aes_decrypt(aes_key, blob.iv, blob.tag, ciphertext,
                                 preamble)
        assert str(decrypted) == snowden1

    @defer.inlineCallbacks
    def test_blob_decryptor(self):

        inf = BytesIO(snowden1)

        blob = _crypto.BlobEncryptor(
            self.doc_info, inf,
            secret='A' * 96)
        ciphertext = yield blob.encrypt()

        decryptor = _crypto.BlobDecryptor(
            self.doc_info, ciphertext,
            secret='A' * 96)
        decrypted = yield decryptor.decrypt()
        assert decrypted == snowden1

    @defer.inlineCallbacks
    def test_encrypt_and_decrypt(self):
        """
        Check that encrypting and decrypting gives same doc.
        """
        crypto = _crypto.SoledadCrypto('A' * 96)
        payload = {'key': 'someval'}
        doc1 = SoledadDocument('id1', '1', json.dumps(payload))

        encrypted = yield crypto.encrypt_doc(doc1)
        assert encrypted != payload
        assert 'raw' in encrypted
        doc2 = SoledadDocument('id1', '1')
        doc2.set_json(encrypted)
        assert _crypto.is_symmetrically_encrypted(encrypted)
        decrypted = yield crypto.decrypt_doc(doc2)
        assert len(decrypted) != 0
        assert json.loads(decrypted) == payload

    @defer.inlineCallbacks
    def test_decrypt_with_wrong_tag_raises(self):
        """
        Trying to decrypt a document with wrong MAC should raise.
        """
        crypto = _crypto.SoledadCrypto('A' * 96)
        payload = {'key': 'someval'}
        doc1 = SoledadDocument('id1', '1', json.dumps(payload))

        encrypted = yield crypto.encrypt_doc(doc1)
        encdict = json.loads(encrypted)
        preamble, raw = _crypto._split(str(encdict['raw']))
        # mess with tag
        messed = raw[:-16] + '0' * 16

        preamble = base64.urlsafe_b64encode(preamble)
        newraw = preamble + ' ' + base64.urlsafe_b64encode(str(messed))
        doc2 = SoledadDocument('id1', '1')
        doc2.set_json(json.dumps({"raw": str(newraw)}))

        with pytest.raises(_crypto.InvalidBlob):
            yield crypto.decrypt_doc(doc2)


class RecoveryDocumentTestCase(BaseSoledadTest):

    def test_export_recovery_document_raw(self):
        rd = self._soledad.secrets._export_recovery_document()
        secret_id = rd[self._soledad.secrets.STORAGE_SECRETS_KEY].items()[0][0]
        # assert exported secret is the same
        secret = self._soledad.secrets._decrypt_storage_secret_version_1(
            rd[self._soledad.secrets.STORAGE_SECRETS_KEY][secret_id])
        self.assertEqual(secret_id, self._soledad.secrets._secret_id)
        self.assertEqual(secret, self._soledad.secrets._secrets[secret_id])
        # assert recovery document structure
        encrypted_secret = rd[
            self._soledad.secrets.STORAGE_SECRETS_KEY][secret_id]
        self.assertTrue(self._soledad.secrets.CIPHER_KEY in encrypted_secret)
        self.assertEquals(
            _crypto.ENC_METHOD.aes_256_gcm,
            encrypted_secret[self._soledad.secrets.CIPHER_KEY])
        self.assertTrue(self._soledad.secrets.LENGTH_KEY in encrypted_secret)
        self.assertTrue(self._soledad.secrets.SECRET_KEY in encrypted_secret)

    def test_import_recovery_document(self, cipher='aes256'):
        rd = self._soledad.secrets._export_recovery_document(cipher)
        s = self._soledad_instance()
        s.secrets._import_recovery_document(rd)
        s.secrets.set_secret_id(self._soledad.secrets._secret_id)
        self.assertEqual(self._soledad.storage_secret,
                         s.storage_secret,
                         'Failed settinng secret for symmetric encryption.')
        s.close()

    def test_import_GCM_recovery_document(self):
        cipher = self._soledad.secrets.CIPHER_AES256_GCM
        self.test_import_recovery_document(cipher)

    def test_import_legacy_CTR_recovery_document(self):
        cipher = self._soledad.secrets.CIPHER_AES256
        self.test_import_recovery_document(cipher)


class SoledadSecretsTestCase(BaseSoledadTest):

    def test_new_soledad_instance_generates_one_secret(self):
        self.assertTrue(
            self._soledad.storage_secret is not None,
            "Expected secret to be something different than None")
        number_of_secrets = len(self._soledad.secrets._secrets)
        self.assertTrue(
            number_of_secrets == 1,
            "Expected exactly 1 secret, got %d instead." % number_of_secrets)

    def test_generated_secret_is_of_correct_type(self):
        expected_type = str
        self.assertIsInstance(
            self._soledad.storage_secret, expected_type,
            "Expected secret to be of type %s" % expected_type)

    def test_generated_secret_has_correct_lengt(self):
        expected_length = self._soledad.secrets.GEN_SECRET_LENGTH
        actual_length = len(self._soledad.storage_secret)
        self.assertTrue(
            expected_length == actual_length,
            "Expected secret with length %d, got %d instead."
            % (expected_length, actual_length))

    def test_generated_secret_id_is_sha256_hash_of_secret(self):
        generated = self._soledad.secrets.secret_id
        expected = hashlib.sha256(self._soledad.storage_secret).hexdigest()
        self.assertTrue(
            generated == expected,
            "Expeceted generated secret id to be sha256 hash, got something "
            "else instead.")

    def test_generate_new_secret_generates_different_secret_id(self):
        # generate new secret
        secret_id_1 = self._soledad.secrets.secret_id
        secret_id_2 = self._soledad.secrets._gen_secret()
        self.assertTrue(
            len(self._soledad.secrets._secrets) == 2,
            "Expected exactly 2 secrets.")
        self.assertTrue(
            secret_id_1 != secret_id_2,
            "Expected IDs of secrets to be distinct.")
        self.assertTrue(
            secret_id_1 in self._soledad.secrets._secrets,
            "Expected to find ID of first secret in Soledad Secrets.")
        self.assertTrue(
            secret_id_2 in self._soledad.secrets._secrets,
            "Expected to find ID of second secret in Soledad Secrets.")

    def test__has_secret(self):
        self.assertTrue(
            self._soledad._secrets._has_secret(),
            "Should have a secret at this point")


class SoledadCryptoAESTestCase(BaseSoledadTest):

    def test_encrypt_decrypt_sym(self):
        # generate 256-bit key
        key = os.urandom(32)
        iv, cyphertext = _crypto.encrypt_sym('data', key)
        self.assertTrue(cyphertext is not None)
        self.assertTrue(cyphertext != '')
        self.assertTrue(cyphertext != 'data')
        plaintext = _crypto.decrypt_sym(cyphertext, key, iv)
        self.assertEqual('data', plaintext)

    def test_decrypt_with_wrong_iv_raises(self):
        key = os.urandom(32)
        iv, cyphertext = _crypto.encrypt_sym('data', key)
        self.assertTrue(cyphertext is not None)
        self.assertTrue(cyphertext != '')
        self.assertTrue(cyphertext != 'data')
        # get a different iv by changing the first byte
        rawiv = binascii.a2b_base64(iv)
        wrongiv = rawiv
        while wrongiv == rawiv:
            wrongiv = os.urandom(1) + rawiv[1:]
        with pytest.raises(InvalidTag):
            _crypto.decrypt_sym(
                cyphertext, key, iv=binascii.b2a_base64(wrongiv))

    def test_decrypt_with_wrong_key_raises(self):
        key = os.urandom(32)
        iv, cyphertext = _crypto.encrypt_sym('data', key)
        self.assertTrue(cyphertext is not None)
        self.assertTrue(cyphertext != '')
        self.assertTrue(cyphertext != 'data')
        wrongkey = os.urandom(32)  # 256-bits key
        # ensure keys are different in case we are extremely lucky
        while wrongkey == key:
            wrongkey = os.urandom(32)
        with pytest.raises(InvalidTag):
            _crypto.decrypt_sym(cyphertext, wrongkey, iv)


def _aes_encrypt(key, iv, data):
    backend = default_backend()
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv), backend=backend)
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize(), encryptor.tag


def _aes_decrypt(key, iv, tag, data, aead=''):
    backend = default_backend()
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag), backend=backend)
    decryptor = cipher.decryptor()
    if aead:
        decryptor.authenticate_additional_data(aead)
    return decryptor.update(data) + decryptor.finalize()
