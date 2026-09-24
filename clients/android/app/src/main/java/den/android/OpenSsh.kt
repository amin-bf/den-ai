package den.android

import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/**
 * The tunnel's ed25519 key in OpenSSH's own formats, with no Android dependency so it can be
 * unit-tested on the JVM: the private key as an unencrypted `openssh-key-v1` file (the device
 * keeps it encrypted with a Keystore key, see [DeviceKey]), the public key as an
 * `authorized_keys` entry, and SHA256 fingerprints the way `ssh-keygen -l` prints them.
 */
object OpenSsh {
    const val KEY_TYPE = "ssh-ed25519"
    const val COMMENT = "den-android"

    /** The broker's own address on its machine; the tunnel may reach nothing else. */
    const val BROKER_HOST = "127.0.0.1"
    const val BROKER_PORT = 11435

    class KeyPair(val privateKeyFile: String, val publicKeyLine: String)

    fun generate(random: SecureRandom = SecureRandom()): KeyPair {
        val private = Ed25519PrivateKeyParameters(random)
        val seed = private.encoded
        val public = private.generatePublicKey().encoded
        return KeyPair(privateKeyFile(seed, public), publicKeyLine(public))
    }

    fun publicKeyBlob(public: ByteArray): ByteArray = sshBytes {
        string(KEY_TYPE.toByteArray())
        string(public)
    }

    fun publicKeyLine(public: ByteArray): String =
        "$KEY_TYPE ${Base64.getEncoder().encodeToString(publicKeyBlob(public))} $COMMENT"

    /** The line for the broker machine's `authorized_keys`: this key may only forward to the broker. */
    fun authorizedKeysLine(publicKeyLine: String, brokerPort: Int = BROKER_PORT): String =
        "restrict,port-forwarding,permitopen=\"$BROKER_HOST:$brokerPort\",command=\"/usr/bin/false\" $publicKeyLine"

    /** `SHA256:…` of a public key blob, as OpenSSH shows it. */
    fun fingerprint(blob: ByteArray): String =
        "SHA256:" + Base64.getEncoder().withoutPadding()
            .encodeToString(MessageDigest.getInstance("SHA-256").digest(blob))

    fun fingerprintOfLine(publicKeyLine: String): String =
        fingerprint(Base64.getDecoder().decode(publicKeyLine.trim().split(" ")[1]))

    /** An unencrypted openssh-key-v1 file (PROTOCOL.key in OpenSSH's sources). */
    fun privateKeyFile(seed: ByteArray, public: ByteArray, random: SecureRandom = SecureRandom()): String {
        val check = random.nextInt()
        val section = ByteArrayOutputStream().apply {
            write(sshBytes {
                int(check)
                int(check)
                string(KEY_TYPE.toByteArray())
                string(public)
                string(seed + public)
                string(COMMENT.toByteArray())
            })
            var pad = 1
            while (size() % 8 != 0) write(pad++)
        }.toByteArray()
        val body = "openssh-key-v1".toByteArray() + byteArrayOf(0) + sshBytes {
            string("none".toByteArray())
            string("none".toByteArray())
            string(ByteArray(0))
            int(1)
            string(publicKeyBlob(public))
            string(section)
        }
        val text = Base64.getEncoder().encodeToString(body).chunked(70).joinToString("\n")
        return "-----BEGIN OPENSSH PRIVATE KEY-----\n$text\n-----END OPENSSH PRIVATE KEY-----\n"
    }

    private class SshWriter(val out: DataOutputStream) {
        fun int(value: Int) = out.writeInt(value)
        fun string(value: ByteArray) {
            out.writeInt(value.size)
            out.write(value)
        }
    }

    private fun sshBytes(block: SshWriter.() -> Unit): ByteArray {
        val bytes = ByteArrayOutputStream()
        DataOutputStream(bytes).use { SshWriter(it).block() }
        return bytes.toByteArray()
    }
}
