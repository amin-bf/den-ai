package den.android

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Where the broker is and how to reach it, kept in the app's private preferences. */
class Settings(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("den", Context.MODE_PRIVATE)

    var transport: String
        get() = prefs.getString("transport", DIRECT) ?: DIRECT
        set(v) = prefs.edit().putString("transport", v).apply()
    var url: String
        get() = prefs.getString("url", DEFAULT_URL) ?: DEFAULT_URL
        set(v) = prefs.edit().putString("url", v).apply()
    var host: String
        get() = prefs.getString("host", "") ?: ""
        set(v) = prefs.edit().putString("host", v).apply()
    var port: Int
        get() = prefs.getInt("port", 22)
        set(v) = prefs.edit().putInt("port", v).apply()
    var user: String
        get() = prefs.getString("user", "") ?: ""
        set(v) = prefs.edit().putString("user", v).apply()

    /** Qwen-style templates think unless this is off; den's own delegation turns it off too. */
    var thinking: Boolean
        get() = prefs.getBoolean("thinking", false)
        set(v) = prefs.edit().putBoolean("thinking", v).apply()

    /** Stream the answer even when the tool is offered; off asks for one whole message. */
    var streamWithTools: Boolean
        get() = prefs.getBoolean("stream_with_tools", true)
        set(v) = prefs.edit().putBoolean("stream_with_tools", v).apply()

    /** The broker's port on the SSH server's loopback; den's default unless it listens elsewhere. */
    var brokerPort: Int
        get() = prefs.getInt("broker_port", OpenSsh.BROKER_PORT)
        set(v) = prefs.edit().putInt("broker_port", v).apply()

    /** The host key fingerprint trusted on first use for host:port, or null. */
    fun pinnedHostKey(host: String, port: Int): String? = prefs.getString("hostkey:$host:$port", null)
    fun pinHostKey(host: String, port: Int, fingerprint: String) =
        prefs.edit().putString("hostkey:$host:$port", fingerprint).apply()
    fun forgetHostKey(host: String, port: Int) = prefs.edit().remove("hostkey:$host:$port").apply()

    val deviceKey = DeviceKey(prefs)

    companion object {
        const val DIRECT = "direct"
        const val SSH = "ssh"
        const val DEFAULT_URL = "http://10.0.2.2:11435"
    }
}

/**
 * The tunnel's own ed25519 key, made on this device. The private key file is stored encrypted
 * (AES-GCM) with a key that never leaves the Android Keystore; only its public half is shown.
 */
class DeviceKey(private val prefs: SharedPreferences) {
    val publicKeyLine: String?
        get() = prefs.getString("ssh_public", null)

    fun ensure(): String {
        publicKeyLine?.let { if (prefs.contains("ssh_private")) return it }
        val pair = OpenSsh.generate()
        prefs.edit()
            .putString("ssh_private", encrypt(pair.privateKeyFile.toByteArray()))
            .putString("ssh_public", pair.publicKeyLine)
            .apply()
        return pair.publicKeyLine
    }

    fun privateKeyFile(): String {
        val stored = prefs.getString("ssh_private", null) ?: error("no SSH key yet; create one first")
        return String(decrypt(stored))
    }

    /** A new key; the old one's authorized_keys line must then be replaced on the server. */
    fun replace() {
        prefs.edit().remove("ssh_private").remove("ssh_public").apply()
        ensure()
    }

    private fun wrappingKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: ByteArray): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, wrappingKey())
        val sealed = cipher.iv + cipher.doFinal(plain)
        return Base64.encodeToString(sealed, Base64.NO_WRAP)
    }

    private fun decrypt(stored: String): ByteArray {
        val sealed = Base64.decode(stored, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, wrappingKey(), GCMParameterSpec(128, sealed, 0, IV_BYTES))
        return cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
    }

    private companion object {
        const val ALIAS = "den-ssh-key-wrap"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
    }
}
