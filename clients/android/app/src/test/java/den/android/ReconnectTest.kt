package den.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.IOException

/** A session that answers, dies, or refuses to open, as a test needs. */
private class FakeSession(var alive: Boolean = true, val failWith: IOException? = null) : BrokerSession {
    /** Set to make this session die the next time it is used. */
    var dieWith: IOException? = null
    override val label = "(test session)"
    override val isOpen: Boolean get() = alive
    var requests = 0
    var closed = false

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        requests++
        (failWith ?: dieWith)?.let {
            alive = false // what a dead SSH session looks like: it fails and is no longer open
            throw it
        }
        return Response(200, ByteArrayInputStream("{}".toByteArray()))
    }

    override fun close() {
        closed = true
        alive = false
    }
}

class ReconnectTest {
    private fun request(transport: Transport) = transport.open("GET", "/status", emptyMap(), null, 1000)

    @Test
    fun aSessionThatDiedWhileIdleIsReopenedForTheNextRequest() {
        val sessions = mutableListOf<FakeSession>()
        val states = mutableListOf<ConnectionState>()
        val transport = Reconnecting(connect = { FakeSession().also { sessions += it } }, onState = { states += it })
        request(transport).use { assertEquals(200, it.status) }
        sessions[0].alive = false // Wi-Fi went, the phone slept, the den restarted…
        request(transport).use { assertEquals(200, it.status) }
        assertEquals(2, sessions.size)
        assertEquals(
            listOf(ConnectionState.RECONNECTING, ConnectionState.CONNECTED, ConnectionState.RECONNECTING, ConnectionState.CONNECTED),
            states,
        )
    }

    @Test
    fun aSessionThatDiesDuringARequestGetsOneMoreGoOnAFreshOne() {
        val sessions = mutableListOf<FakeSession>()
        var next: IOException? = null
        val transport = Reconnecting(connect = { FakeSession(failWith = next).also { sessions += it } })
        request(transport).use { assertEquals(200, it.status) }
        // The first session will now die the moment it is used; the new one must answer.
        sessions[0].dieWith = IOException("the SSH connection is closed; reconnect")
        request(transport).use { assertEquals(200, it.status) }
        assertEquals(2, sessions.size)
        assertEquals(2, sessions[0].requests)
        assertEquals(1, sessions[1].requests)
        assertTrue(sessions[0].closed)
    }

    @Test
    fun aFailureOnAFreshSessionIsNotRepeated() {
        val sessions = mutableListOf<FakeSession>()
        val transport = Reconnecting(connect = { FakeSession(failWith = IOException("host unreachable")).also { sessions += it } })
        try {
            request(transport)
            fail("expected the failure to come through")
        } catch (e: IOException) {
            assertEquals("host unreachable", e.message)
        }
        assertEquals(1, sessions.size)
    }

    @Test
    fun aFailureOnASessionThatIsStillUpIsNotRepeated() {
        // The den answered badly rather than the line dying: repeating could run it twice.
        var opened = 0
        val alive = object : BrokerSession {
            override val label = "(alive)"
            override val isOpen = true
            var requests = 0
            override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
                requests++
                if (requests == 1) return Response(200, ByteArrayInputStream("{}".toByteArray()))
                throw IOException("the body ended early")
            }
            override fun close() {}
        }
        val transport = Reconnecting(connect = { opened++; alive })
        request(transport).use { assertEquals(200, it.status) }
        try {
            request(transport)
            fail("expected the failure to come through")
        } catch (e: IOException) {
            assertEquals("the body ended early", e.message)
        }
        assertEquals(1, opened)
        assertEquals(2, alive.requests)
    }

    @Test
    fun theSecondReconnectFailingSaysSo() {
        var opened = 0
        val first = FakeSession()
        val transport = Reconnecting(
            connect = {
                opened++
                when (opened) {
                    1 -> first
                    else -> throw IOException("host unreachable")
                }
            },
        )
        request(transport).use { assertEquals(200, it.status) }
        first.alive = false // the session died while nothing was happening
        try {
            request(transport)
            fail("expected the reconnect's failure")
        } catch (e: IOException) {
            assertEquals("host unreachable", e.message)
        }
        assertEquals(2, opened)
    }
}

class ForegroundTest {
    @Test
    fun reopenOpensASessionWhenThereIsNone() {
        var opened = 0
        val transport = Reconnecting(connect = { opened++; FakeSession() })
        transport.reopen()
        assertEquals(1, opened)
        assertTrue(transport.isOpen)
        // Coming back while the session is still good costs nothing.
        transport.reopen()
        assertEquals(1, opened)
    }

    @Test
    fun reopenReplacesADeadSession() {
        val sessions = mutableListOf<FakeSession>()
        val states = mutableListOf<ConnectionState>()
        val transport = Reconnecting(connect = { FakeSession().also { sessions += it } }, onState = { states += it })
        transport.reopen()
        sessions[0].alive = false // it died while the app was in the background
        transport.reopen()
        assertEquals(2, sessions.size)
        assertTrue(sessions[0].closed)
        assertTrue(transport.isOpen)
        assertEquals(
            listOf(ConnectionState.RECONNECTING, ConnectionState.CONNECTED, ConnectionState.RECONNECTING, ConnectionState.CONNECTED),
            states,
        )
    }

    @Test
    fun aFailedReopenSaysWhy() {
        val transport = Reconnecting(connect = { throw IOException("host unreachable") })
        try {
            transport.reopen()
            fail("expected the failure")
        } catch (e: IOException) {
            assertEquals("host unreachable", e.message)
        }
        assertTrue(!transport.isOpen)
    }
}
