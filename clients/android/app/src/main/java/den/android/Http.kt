package den.android

import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/** A response whose body is read as a stream; close() releases the connection or channel. */
class Response(val status: Int, val body: InputStream, private val onClose: () -> Unit = {}) : Closeable {
    override fun close() = onClose()
}

/** How DenClient reaches the broker: plain HTTP (development) or a channel through SSH. */
interface Transport {
    /** Where requests go, for error messages. */
    val label: String

    fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response
}

/**
 * Just enough HTTP/1.1 to talk to the broker over a raw byte stream (an SSH direct-tcpip
 * channel): one request per connection, `Connection: close`, and a body read by
 * Content-Length, chunked transfer (the NDJSON stream of /image) or until the end.
 */
object Http {
    fun writeRequest(out: OutputStream, method: String, path: String, host: String, headers: Map<String, String>, body: ByteArray?) {
        val head = StringBuilder("$method $path HTTP/1.1\r\n")
        head.append("Host: $host\r\n")
        headers.forEach { (k, v) -> head.append("$k: $v\r\n") }
        if (body != null) head.append("Content-Length: ${body.size}\r\n")
        head.append("Connection: close\r\n\r\n")
        out.write(head.toString().toByteArray(Charsets.ISO_8859_1))
        if (body != null) out.write(body)
        out.flush()
    }

    /** Reads the status line and headers; the returned body stream stops where the body does. */
    fun readResponse(input: InputStream, onClose: () -> Unit = {}): Response {
        val statusLine = readLine(input) ?: throw IOException("the connection closed before a response")
        val parts = statusLine.split(" ", limit = 3)
        if (parts.size < 2 || !parts[0].startsWith("HTTP/")) throw IOException("not an HTTP response: ${statusLine.take(80)}")
        val status = parts[1].toIntOrNull() ?: throw IOException("bad status line: ${statusLine.take(80)}")
        val headers = mutableMapOf<String, String>()
        while (true) {
            val line = readLine(input) ?: throw IOException("the connection closed inside the headers")
            if (line.isEmpty()) break
            val colon = line.indexOf(':')
            if (colon > 0) headers[line.substring(0, colon).trim().lowercase()] = line.substring(colon + 1).trim()
        }
        val body = when {
            headers["transfer-encoding"]?.lowercase()?.contains("chunked") == true -> ChunkedInputStream(input)
            headers["content-length"] != null -> BoundedInputStream(
                input, headers["content-length"]!!.toLongOrNull() ?: throw IOException("bad Content-Length"),
            )
            else -> input
        }
        return Response(status, body, onClose)
    }

    /** A CRLF- (or LF-) terminated line without its ending, or null at the end of the stream. */
    internal fun readLine(input: InputStream): String? {
        val bytes = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b == -1) return if (bytes.size() == 0) null else bytes.toString(Charsets.ISO_8859_1.name())
            if (b == '\n'.code) break
            bytes.write(b)
            if (bytes.size() > 64 * 1024) throw IOException("header line too long")
        }
        val line = bytes.toByteArray()
        val end = if (line.isNotEmpty() && line.last() == '\r'.code.toByte()) line.size - 1 else line.size
        return String(line, 0, end, Charsets.ISO_8859_1)
    }

    private class BoundedInputStream(private val input: InputStream, private var left: Long) : InputStream() {
        override fun read(): Int {
            if (left <= 0) return -1
            val b = input.read()
            if (b == -1) throw IOException("the body ended $left bytes early")
            left--
            return b
        }

        override fun read(buf: ByteArray, off: Int, len: Int): Int {
            if (left <= 0) return -1
            val n = input.read(buf, off, minOf(len.toLong(), left).toInt())
            if (n == -1) throw IOException("the body ended $left bytes early")
            left -= n
            return n
        }
    }

    private class ChunkedInputStream(private val input: InputStream) : InputStream() {
        private var left = 0L
        private var done = false

        private fun nextChunk(): Boolean {
            if (done) return false
            if (left > 0) return true
            val sizeLine = readLine(input) ?: throw IOException("the stream ended inside a chunked body")
            val size = sizeLine.substringBefore(';').trim().toLongOrNull(16)
                ?: throw IOException("bad chunk size: ${sizeLine.take(20)}")
            if (size == 0L) {
                // Trailers, if any, end with an empty line.
                while (!(readLine(input) ?: "").isEmpty()) Unit
                done = true
                return false
            }
            left = size
            return true
        }

        private fun endOfChunk() {
            if (left == 0L && readLine(input) != "") throw IOException("a chunk didn't end with CRLF")
        }

        override fun read(): Int {
            if (!nextChunk()) return -1
            val b = input.read()
            if (b == -1) throw IOException("the stream ended inside a chunk")
            left--
            endOfChunk()
            return b
        }

        override fun read(buf: ByteArray, off: Int, len: Int): Int {
            if (len == 0) return 0
            if (!nextChunk()) return -1
            val n = input.read(buf, off, minOf(len.toLong(), left).toInt())
            if (n == -1) throw IOException("the stream ended inside a chunk")
            left -= n
            endOfChunk()
            return n
        }
    }
}
