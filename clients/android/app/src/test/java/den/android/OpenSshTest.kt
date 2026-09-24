package den.android

import net.schmizz.sshj.common.Buffer
import com.hierynomus.sshj.signature.SignatureEdDSA
import com.hierynomus.sshj.userauth.keyprovider.OpenSSHKeyV1KeyFile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

class OpenSshTest {
    private fun load(pair: OpenSsh.KeyPair) = OpenSSHKeyV1KeyFile().apply { init(pair.privateKeyFile, null) }

    @Test
    fun privateKeyFileLoadsInSshjWithTheSamePublicKey() {
        val pair = OpenSsh.generate()
        val key = load(pair)
        val blob = Buffer.PlainBuffer().putPublicKey(key.public).compactData
        assertEquals(pair.publicKeyLine.split(" ")[1], Base64.getEncoder().encodeToString(blob))
    }

    @Test
    fun loadedKeySignsWhatItsPublicKeyVerifies() {
        val key = load(OpenSsh.generate())
        val data = "den".toByteArray()
        val signer = SignatureEdDSA.Factory().create().apply { initSign(key.private) }
        signer.update(data)
        val signature = signer.sign()
        val verifier = SignatureEdDSA.Factory().create().apply { initVerify(key.public) }
        verifier.update(data)
        assertTrue(verifier.verify(Buffer.PlainBuffer().putString(OpenSsh.KEY_TYPE).putBytes(signature).compactData))
    }

    @Test
    fun authorizedKeysLineOnlyForwardsToTheBroker() {
        val line = OpenSsh.authorizedKeysLine("ssh-ed25519 AAAA den-android")
        assertEquals(
            "restrict,port-forwarding,permitopen=\"127.0.0.1:11435\",command=\"/usr/bin/false\" ssh-ed25519 AAAA den-android",
            line,
        )
    }

    @Test
    fun fingerprintMatchesOpenSshFormat() {
        // ssh-keygen -lf of this public key prints this fingerprint.
        val line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test"
        assertEquals(FINGERPRINT, OpenSsh.fingerprintOfLine(line))
    }

    companion object {
        const val FINGERPRINT = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
    }
}
