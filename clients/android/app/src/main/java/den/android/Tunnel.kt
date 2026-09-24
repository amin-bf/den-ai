package den.android

import net.schmizz.keepalive.KeepAliveProvider
import net.schmizz.sshj.DefaultConfig
import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.common.Buffer
import net.schmizz.sshj.connection.channel.OpenFailException
import net.schmizz.sshj.transport.verification.HostKeyVerifier
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.io.BufferedInputStream
import java.io.IOException
import java.security.PublicKey
import java.security.Security
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** The server's host key isn't pinned yet (trust on first use) or no longer matches the pin. */
class HostKeyException(val fingerprint: String, val changed: Boolean) : Exception(
    if (changed) "HOST KEY CHANGED: the server now shows $fingerprint, not the pinned key. " +
        "If you didn't change it there, someone may be in between; don't trust it."
    else "unknown host key $fingerprint; compare it with the server's (ssh-keygen -lf on its host key) and trust it"
)

/** A session to the broker that a client can reopen when it dies. */
interface BrokerSession : Transport {
    val isOpen: Boolean
    fun close()
}

/** What the app shows about its connection. */
enum class ConnectionState { CONNECTED, RECONNECTING, DISCONNECTED }

/**
 * A transport that keeps one session and opens a new one when the old has died — a phone loses
 * Wi-Fi, sleeps, changes network, and an idle SSH session goes quietly away. A request that
 * found a dead session is tried once more on a fresh one; a request that failed on a session
 * that is still up is not repeated, because the den may already have run it. Nothing polls and
 * nothing is held awake: the reconnect happens when there is something to send.
 */
class Reconnecting(
    private val connect: () -> BrokerSession,
    private val onState: (ConnectionState) -> Unit = {},
) : Transport {
    private var session: BrokerSession? = null

    override val label: String get() = session?.label ?: "through the SSH session"

    val isOpen: Boolean get() = session?.isOpen == true

    @Synchronized
    private fun session(): BrokerSession {
        session?.takeIf { it.isOpen }?.let { return it }
        session?.close()
        session = null
        onState(ConnectionState.RECONNECTING)
        val fresh = connect()
        session = fresh
        onState(ConnectionState.CONNECTED)
        return fresh
    }

    /** Open a session now if there is none, e.g. when the app comes back to the foreground. */
    fun reopen() {
        session()
    }

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        val existing = synchronized(this) { session?.takeIf { it.isOpen } }
        val session = existing ?: session()
        return try {
            session.open(method, path, headers, body, timeoutMs)
        } catch (e: IOException) {
            // Only a session that has since died gets a second go: then nothing was sent.
            if (existing == null || session.isOpen) {
                if (!session.isOpen) onState(ConnectionState.DISCONNECTED)
                throw e
            }
            synchronized(this) { if (this.session === session) this.session = null }
            session.close()
            try {
                session().open(method, path, headers, body, timeoutMs)
            } catch (again: Exception) {
                onState(ConnectionState.DISCONNECTED)
                throw again
            }
        }
    }

    fun close() {
        synchronized(this) {
            session?.close()
            session = null
        }
        onState(ConnectionState.DISCONNECTED)
    }
}

/**
 * An SSH session to the broker's machine that reaches the broker's 127.0.0.1:[remotePort]
 * there (ADR 0007). Each request opens its own direct-tcpip channel, like `ssh -W`, and closes
 * it after the response, so nothing listens on this device: no other app can use the session.
 * The key may do nothing else (restrict, permitopen, command=/usr/bin/false).
 */
class Tunnel private constructor(private val ssh: SSHClient, private val remotePort: Int) : BrokerSession {
    override val label: String get() = "through the SSH session (broker port $remotePort there)"
    override val isOpen: Boolean get() = ssh.isConnected && ssh.isAuthenticated

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        if (!isOpen) throw IOException("the SSH connection is closed; reconnect")
        val channel = try {
            ssh.newDirectConnection(OpenSsh.BROKER_HOST, remotePort)
        } catch (e: OpenFailException) {
            throw IOException(
                "the server refused a channel to ${OpenSsh.BROKER_HOST}:$remotePort (${e.message}); " +
                    "the key's authorized_keys line there must permit that port",
            )
        }
        // The channel has no read timeout of its own: close it once the request's time is up.
        val deadline = WATCHDOG.schedule({ runCatching { channel.close() } }, timeoutMs.toLong(), TimeUnit.MILLISECONDS)
        val close = {
            deadline.cancel(false)
            runCatching { channel.close() }
            Unit
        }
        try {
            Http.writeRequest(channel.outputStream, method, path, "${OpenSsh.BROKER_HOST}:$remotePort", headers, body)
            return Http.readResponse(BufferedInputStream(channel.inputStream), close)
        } catch (e: Exception) {
            close()
            throw e as? IOException ?: IOException(e.message ?: e.javaClass.simpleName, e)
        }
    }

    override fun close() {
        runCatching { ssh.disconnect() }
    }

    companion object {
        private val WATCHDOG = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "den-ssh-deadline").apply { isDaemon = true } }

        init {
            // Android's own "BC" provider is a cut-down copy that lacks what sshj needs
            // (X25519, Ed25519); put the full Bouncy Castle in its place.
            Security.removeProvider("BC")
            Security.addProvider(BouncyCastleProvider())
        }

        /** Blocking; call it off the main thread. [pinned] is the trusted fingerprint, if any. */
        fun open(host: String, port: Int, user: String, privateKeyFile: String, pinned: String?, remotePort: Int): Tunnel {
            // A keep-alive that expects an answer, so a session that died is noticed in seconds
            // instead of on the next message.
            val config = DefaultConfig().apply { keepAliveProvider = KeepAliveProvider.KEEP_ALIVE }
            val ssh = SSHClient(config)
            var seen: String? = null
            ssh.addHostKeyVerifier(object : HostKeyVerifier {
                override fun verify(hostname: String, port: Int, key: PublicKey): Boolean {
                    seen = OpenSsh.fingerprint(Buffer.PlainBuffer().putPublicKey(key).compactData)
                    return seen == pinned
                }

                override fun findExistingAlgorithms(hostname: String, port: Int): List<String> = emptyList()
            })
            ssh.connectTimeout = 10_000
            try {
                ssh.connect(host, port)
            } catch (e: Exception) {
                runCatching { ssh.disconnect() }
                seen?.takeIf { it != pinned }?.let { throw HostKeyException(it, changed = pinned != null) }
                throw e
            }
            try {
                ssh.connection.keepAlive.keepAliveInterval = 25
                ssh.authPublickey(user, ssh.loadKeys(privateKeyFile, null, null))
                return Tunnel(ssh, remotePort)
            } catch (e: Exception) {
                runCatching { ssh.disconnect() }
                throw e
            }
        }
    }
}
