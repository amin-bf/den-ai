package den.android

import android.content.Context
import android.media.MediaRecorder
import java.io.File

/** Records the microphone to an M4A file in the app's cache: a voice sample for the den (ADR voice-overs). */
class Recorder(private val context: Context) {
    private var recorder: MediaRecorder? = null
    private var file: File? = null
    private var startedAt = 0L

    val recording get() = recorder != null

    fun start() {
        stop()
        val target = File(context.cacheDir, "voice-${System.currentTimeMillis()}.m4a")
        recorder = MediaRecorder(context).apply {
            setAudioSource(MediaRecorder.AudioSource.MIC)
            setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            setAudioChannels(1)
            setAudioSamplingRate(44_100)
            setAudioEncodingBitRate(128_000)
            setOutputFile(target)
            prepare()
            start()
        }
        file = target
        startedAt = System.currentTimeMillis()
    }

    /** Stops and returns the recording with its length in seconds, or null when there was none. */
    fun stop(): Pair<File, Double>? {
        val r = recorder ?: return null
        recorder = null
        val seconds = (System.currentTimeMillis() - startedAt) / 1000.0
        return try {
            r.stop()
            file?.let { it to seconds }
        } catch (e: RuntimeException) {
            // Stopped before any audio was written: nothing to keep.
            file?.delete()
            null
        } finally {
            r.release()
            file = null
        }
    }
}
