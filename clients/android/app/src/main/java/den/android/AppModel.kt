package den.android

import android.app.Application
import android.content.ContentValues
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.Base64
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** A setting of /image the form can offer: a scalar property of the broker's JSON schema. */
data class Field(val name: String, val type: String, val choices: List<String>, val description: String)

/** A keyframe being set up: the picture, and its moment as the user types it. */
class ClipKeyframe(val picked: Picked, at: String) {
    var at by mutableStateOf(at)
}

/** A keyframe's moment as /clip takes it (ADR 0009), from what the user typed. */
object ClipMoment {
    /** "start" or empty → 0, "end" → "end", "50%" → "50%", "2.5" or "2.5s" → 2.5 seconds. */
    fun parse(text: String): Any {
        val t = text.trim().lowercase().removeSuffix("s").trim()
        return when {
            t.isEmpty() || t == "start" -> 0
            t == "end" -> "end"
            t.endsWith("%") && t.removeSuffix("%").trim().toDoubleOrNull() != null -> t.removeSuffix("%").trim() + "%"
            t.toDoubleOrNull() != null -> t.toDouble()
            else -> throw DenException("a keyframe's moment is start, end, seconds (2.5) or a percentage (50%); got \"$text\"")
        }
    }
}

/** A file picked on this device: only its name and contents go to the broker. */
class Picked(val name: String, val bytes: ByteArray) {
    val bitmap: Bitmap? by lazy { BitmapFactory.decodeByteArray(bytes, 0, bytes.size) }
}

class AppModel(app: Application) : AndroidViewModel(app) {
    val settings = Settings(app)

    // --- connection ---
    var transport by mutableStateOf(settings.transport)
    var url by mutableStateOf(settings.url)
    var host by mutableStateOf(settings.host)
    var port by mutableStateOf(settings.port.toString())
    var user by mutableStateOf(settings.user)
    var brokerPort by mutableStateOf(settings.brokerPort.toString())
    var publicKey by mutableStateOf(settings.deviceKey.publicKeyLine)
    var connecting by mutableStateOf(false)
    var connectError by mutableStateOf<String?>(null)
    var hostKeyPrompt by mutableStateOf<HostKeyException?>(null)
    var connectedTo by mutableStateOf<String?>(null)
    /** What the connection is doing, so a session that dropped isn't a mystery. */
    var link by mutableStateOf(ConnectionState.DISCONNECTED)
    private var tunnel: Reconnecting? = null
    var client: DenClient? = null
        private set

    // --- status ---
    var status by mutableStateOf<JSONObject?>(null)
    var info by mutableStateOf<JSONObject?>(null)
    var statusError by mutableStateOf<String?>(null)
    var loadingStatus by mutableStateOf(false)

    // --- ask ---
    var task by mutableStateOf<String?>(null)
    var instructions by mutableStateOf("")
    var askText by mutableStateOf("")
    var askFile by mutableStateOf<Picked?>(null)
    var asking by mutableStateOf(false)
    var answer by mutableStateOf<JSONObject?>(null)
    var askError by mutableStateOf<String?>(null)
    var feedbackNote by mutableStateOf("")
    var feedbackSent by mutableStateOf<String?>(null)

    // --- image ---
    var prompt by mutableStateOf("")
    var workflow by mutableStateOf<String?>(null)
    val imageValues = mutableStateMapOf<String, String>()
    var inputImage by mutableStateOf<Picked?>(null)
    var generating by mutableStateOf(false)
    val progress = mutableStateListOf<String>()
    var resultImages by mutableStateOf<List<Bitmap>>(emptyList())
    var resultMaps by mutableStateOf<List<Pair<String, Bitmap>>>(emptyList())
    var resultSummary by mutableStateOf<String?>(null)
    var imageError by mutableStateOf<String?>(null)

    // --- clip (ADR 0009) ---
    var clipMode by mutableStateOf(false)
    var clipPrompt by mutableStateOf("")
    var clipNegative by mutableStateOf("")
    var clipWorkflow by mutableStateOf<String?>(null)
    var clipDuration by mutableStateOf("")
    var clipSize by mutableStateOf("")
    /** A voice-over: an SRT script (loaded from a file or typed) or a line of text, empty for none. */
    var clipVoiceover by mutableStateOf("")
    var clipVoice by mutableStateOf<String?>(null)
    var clipLanguage by mutableStateOf<String?>(null)
    var voiceNote by mutableStateOf<String?>(null)
    /** Make the clip to the voice-over, so a person on screen speaks it; where the workflow can. */
    var clipLipSync by mutableStateOf(false)
    /** The script at the times the last clip's lines were spoken, to reuse or keep. */
    var lastSrt by mutableStateOf<String?>(null)
    private val recorder = Recorder(app)
    var recording by mutableStateOf(false)
    /** What the recorder is recording: a new voice's sample, or the narration of a clip. */
    var recordingNarration by mutableStateOf(false)
    /** The user's own narration, recorded here; its words come back as the timed script. */
    var narration by mutableStateOf<Picked?>(null)
    /** The narration itself is the voice-over, instead of a voice reading its script. */
    var useNarration by mutableStateOf(false)
    /** A sound track, on a workflow that makes sound; sent only when switched off. */
    var clipSound by mutableStateOf(true)
    /** LoRAs picked for the clip, each with its strength as typed; empty for the LoRA's default. */
    val clipLoras = mutableStateMapOf<String, String>()
    /** Images the clip must show, each with its moment as typed: start, end, seconds or NN%. */
    val clipKeyframes = mutableStateListOf<ClipKeyframe>()
    /** The clip being made. It goes on while the app is away (a detached request), so the id stays. */
    var clipId by mutableStateOf<Int?>(null)
    var clipState by mutableStateOf<String?>(null)
    var clipSheet by mutableStateOf<Bitmap?>(null)
    var clipUri by mutableStateOf<Uri?>(null)
    var clipSummary by mutableStateOf<String?>(null)
    var clipError by mutableStateOf<String?>(null)
    @Volatile private var watchingClip = false

    // --- chat ---
    val chats = ChatStore(app)
    var conversations by mutableStateOf<List<Conversation>>(emptyList())
    var chat by mutableStateOf<Conversation?>(null)
    /** Bumped whenever an open conversation's messages change, since they are plain objects. */
    var chatTick by mutableStateOf(0)
    var draft by mutableStateOf("")
    /** Images picked for the message being written, already scaled for the model. */
    val pending = mutableStateListOf<Attached>()
    var chatBusy by mutableStateOf(false)
    var chatError by mutableStateOf<String?>(null)
    /** The running turn's Stop, while there is one. */
    private var turnStop: Stop? = null
    var skillList by mutableStateOf<List<JSONObject>>(emptyList())
    var skillsLoading by mutableStateOf(false)
    var skillsError by mutableStateOf<String?>(null)
    var models by mutableStateOf<List<String>>(emptyList())
    var modelsLoading by mutableStateOf(false)
    var thinking by mutableStateOf(settings.thinking)
    var streamWithTools by mutableStateOf(settings.streamWithTools)

    // --- poses ---
    var poseToc by mutableStateOf<String?>(null)
    var poseNames by mutableStateOf<List<String>>(emptyList())
    var pose by mutableStateOf<String?>(null)
    var poseDetails by mutableStateOf<String?>(null)
    var poseImages by mutableStateOf<List<Bitmap>>(emptyList())
    var posesError by mutableStateOf<String?>(null)
    var loadingPoses by mutableStateOf(false)

    fun createKey() = io {
        publicKey = settings.deviceKey.ensure()
    }

    fun replaceKey() = io {
        settings.deviceKey.replace()
        publicKey = settings.deviceKey.publicKeyLine
    }

    fun connect() {
        settings.transport = transport
        settings.url = url.trim()
        settings.host = host.trim()
        settings.port = port.toIntOrNull() ?: 22
        settings.user = user.trim()
        settings.brokerPort = brokerPort.toIntOrNull() ?: OpenSsh.BROKER_PORT
        disconnect()
        connecting = true
        connectError = null
        hostKeyPrompt = null
        io {
            try {
                val c = if (transport == Settings.SSH) {
                    val (h, p) = settings.host to settings.port
                    if (h.isEmpty() || settings.user.isEmpty()) throw DenException("host and user are needed")
                    // The session is opened here, and opened again by itself later if it dies.
                    // The pinned host key is read each time, so a changed key is still refused.
                    val session = Reconnecting(
                        connect = {
                            Tunnel.open(
                                h, p, settings.user, settings.deviceKey.privateKeyFile(),
                                settings.pinnedHostKey(h, p), settings.brokerPort,
                            )
                        },
                        onState = { link = it },
                    )
                    tunnel = session
                    DenClient(session)
                } else {
                    DenClient(settings.url)
                }
                client = c
                loadStatus(c)
                link = ConnectionState.CONNECTED
                connectedTo = if (transport == Settings.SSH) "${settings.user}@${settings.host}:${settings.port} over SSH, broker port ${settings.brokerPort}" else settings.url
            } catch (e: HostKeyException) {
                hostKeyPrompt = e
                connectError = e.message
            } catch (e: net.schmizz.sshj.userauth.UserAuthException) {
                connectError = "the server refused this device's key (${e.message}); add this device's authorized_keys line (above) on the broker's machine"
                disconnect()
            } catch (e: Exception) {
                connectError = e.message ?: e.javaClass.simpleName
                disconnect()
            } finally {
                connecting = false
            }
        }
    }

    /** Pin the host key shown on first use, then connect again. */
    fun trustHostKey() {
        val prompt = hostKeyPrompt ?: return
        if (prompt.changed) return
        settings.pinHostKey(settings.host, settings.port, prompt.fingerprint)
        connect()
    }

    fun forgetHostKey() {
        settings.forgetHostKey(settings.host, settings.port)
        hostKeyPrompt = null
    }

    fun disconnect() {
        tunnel?.close()
        tunnel = null
        link = ConnectionState.DISCONNECTED
        client = null
        connectedTo = null
        status = null
        info = null
        // What came from one den never shows as another's.
        poseToc = null
        poseNames = emptyList()
        pose = null
        poseDetails = null
        poseImages = emptyList()
        answer = null
    }

    /**
     * The app has come back to the foreground. A session left behind while it was away may have
     * died — the phone slept, Wi-Fi changed — so open a new one now, quietly, rather than letting
     * the first message pay for it. Nothing runs while the app is away: no polling, no wake lock,
     * no reconnect in the background.
     */
    fun onForeground() {
        // A clip went on while we were away; look at it again (the transport reopens as needed).
        clipId?.let { id -> client?.let { c -> io { watchClip(c, id) } } }
        val session = tunnel ?: return
        if (connectedTo == null || session.isOpen) return
        if (chatBusy || asking || generating || loadingStatus) return
        link = ConnectionState.RECONNECTING
        connectError = null
        io {
            try {
                session.reopen()
                link = ConnectionState.CONNECTED
            } catch (e: HostKeyException) {
                // The pin still decides: a changed key is refused, never reconnected quietly.
                hostKeyPrompt = e
                connectError = e.message
                link = ConnectionState.DISCONNECTED
            } catch (e: Exception) {
                connectError = e.message ?: e.javaClass.simpleName
                link = ConnectionState.DISCONNECTED
            }
        }
    }

    /** Open the connection again after it failed, without going through the whole form. */
    fun reconnect() {
        if (tunnel != null && connectedTo != null) onForeground() else connect()
    }

    fun refreshStatus() {
        val c = client ?: return
        io { loadStatus(c) }
    }

    private suspend fun loadStatus(c: DenClient) {
        loadingStatus = true
        try {
            val s = c.status()
            val i = c.client()
            status = s
            info = i
            statusError = null
            val tasks = i.optJSONObject("tasks")?.keys()?.asSequence()?.toList().orEmpty()
            if (task !in tasks) task = tasks.firstOrNull()
            val image = i.optJSONObject("image")
            // A workflow picked on another den may not exist on this one.
            val workflows = image?.optJSONArray("workflows")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
            if (workflow !in workflows) workflow = image?.textOrNull("default")
        } catch (e: Exception) {
            statusError = e.message
            if (status == null) throw e
        } finally {
            loadingStatus = false
        }
    }

    // --- ask ---

    fun ask() {
        val c = client ?: return
        val t = task ?: return
        asking = true
        answer = null
        askError = null
        feedbackSent = null
        feedbackNote = ""
        val files = askFile?.let { listOf(it.name to String(it.bytes)) }.orEmpty()
        io {
            try {
                answer = c.delegate(t, instructions, askText.ifBlank { null }, files)
            } catch (e: Exception) {
                askError = e.message
            } finally {
                asking = false
            }
        }
    }

    fun sendFeedback(verdict: String) {
        val c = client ?: return
        val id = answer?.opt("id") ?: return
        io {
            feedbackSent = try {
                c.feedback(id, verdict, feedbackNote)
                "recorded: $verdict"
            } catch (e: Exception) {
                e.message
            }
        }
    }

    // --- image ---

    /** The scalar settings of /image from its JSON schema; paths and nested inputs are left out. */
    fun imageFields(): List<Field> {
        val props = info?.optJSONObject("image")?.optJSONObject("parameters")?.optJSONObject("properties") ?: return emptyList()
        return props.keys().asSequence().filter { it !in ImageTool.NOT_FIELDS }.mapNotNull { name ->
            val p = props.getJSONObject(name)
            val type = p.optString("type")
            if (type !in setOf("string", "integer", "number", "boolean")) return@mapNotNull null
            val choices = p.optJSONArray("enum")?.let { a -> (0 until a.length()).map { a.get(it).toString() } }.orEmpty()
            Field(name, type, choices, p.optString("description"))
        }.toList()
    }

    fun generate() {
        val c = client ?: return
        val request = JSONObject().put("prompt", prompt)
        workflow?.let { request.put("workflow", it) }
        try {
            for (field in imageFields()) {
                val raw = imageValues[field.name]?.trim().orEmpty()
                if (raw.isEmpty()) continue
                request.put(field.name, when (field.type) {
                    "integer" -> raw.toLongOrNull() ?: throw DenException("${field.name} must be a whole number")
                    "number" -> raw.toDoubleOrNull() ?: throw DenException("${field.name} must be a number")
                    "boolean" -> raw == "true"
                    else -> raw
                })
            }
        } catch (e: DenException) {
            imageError = e.message
            return
        }
        inputImage?.let {
            request.put("image", JSONObject().put("name", it.name).put("base64", Base64.encodeToString(it.bytes, Base64.NO_WRAP)))
        }
        generating = true
        imageError = null
        progress.clear()
        resultImages = emptyList()
        resultMaps = emptyList()
        resultSummary = null
        io {
            try {
                val result = c.image(request) { msg ->
                    val line = DenClient.describeProgress(msg)
                    viewModelScope.launch { progress.add(line) }
                }
                val images = result.optJSONArray("images")
                val bytes = (0 until (images?.length() ?: 0)).map { Base64.decode(images!!.getString(it), Base64.DEFAULT) }
                val maps = result.optJSONArray("maps")
                resultImages = bytes.mapNotNull { BitmapFactory.decodeByteArray(it, 0, it.size) }
                resultMaps = (0 until (maps?.length() ?: 0)).mapNotNull { i ->
                    val m = maps!!.getJSONObject(i)
                    val b = Base64.decode(m.getString("base64"), Base64.DEFAULT)
                    BitmapFactory.decodeByteArray(b, 0, b.size)?.let { m.optString("label") to it }
                }
                val summary = result.optJSONArray("summary")?.let { a -> (0 until a.length()).joinToString(" · ") { a.getString(it) } }
                val saved = bytes.map { saveToGallery(it) }
                resultSummary = listOfNotNull(
                    summary,
                    "${result.opt("seconds")} s" + (result.optDouble("waited_s", 0.0).takeIf { it > 0 }?.let { ", waited $it s" } ?: ""),
                    "saved to ${saved.joinToString()}",
                ).joinToString("\n")
            } catch (e: Exception) {
                imageError = e.message
            } finally {
                generating = false
            }
        }
    }

    // --- clip ---

    fun startClip() {
        val c = client ?: return
        val body = JSONObject().put("prompt", clipPrompt)
        clipWorkflow?.let { body.put("workflow", it) }
        // Added to the workflow's own negative by the broker, so empty keeps just that.
        clipNegative.trim().takeIf { it.isNotEmpty() }?.let { body.put("negative", it) }
        // The broker checks the size; left empty, the clip takes the start frame's shape.
        clipSize.trim().takeIf { it.isNotEmpty() }?.let { body.put("size", it) }
        if (clipMakesSound() && !clipSound) body.put("sound", false)
        clipVoiceover.trim().takeIf { it.isNotEmpty() && voiceInfo() != null }?.let { spoken ->
            // A script has timed lines; anything else is one text spoken from the start.
            // A script has timed lines; several lines without times are spoken in turn and timed by
            // the den; one line is a text spoken from the start.
            val voiceover = when {
                "-->" in spoken -> JSONObject().put("srt", spoken)
                "\n" in spoken -> JSONObject().put("lines", JSONArray(spoken.lines().filter { it.isNotBlank() }))
                else -> JSONObject().put("text", spoken)
            }
            clipVoice?.let { voiceover.put("voice", it) }
            clipLanguage?.let { voiceover.put("language", it) }
            if (clipLipSync && clipCanLipSync()) voiceover.put("sync", true)
            narration?.takeIf { useNarration }?.let {
                voiceover.put("audio", JSONObject().put("name", it.name).put("base64", Base64.encodeToString(it.bytes, Base64.NO_WRAP)))
            }
            body.put("voiceover", voiceover)
        }
        clipDuration.trim().takeIf { it.isNotEmpty() }?.let {
            val seconds = it.toDoubleOrNull()
            if (seconds == null) {
                clipError = "duration must be a number of seconds"
                return
            }
            body.put("duration", seconds)
        }
        if (clipKeyframes.isNotEmpty()) {
            val keyframes = JSONArray()
            for (keyframe in clipKeyframes) {
                val at = try {
                    ClipMoment.parse(keyframe.at)
                } catch (e: DenException) {
                    clipError = e.message
                    return
                }
                val image = JSONObject().put("name", keyframe.picked.name)
                    .put("base64", Base64.encodeToString(keyframe.picked.bytes, Base64.NO_WRAP))
                keyframes.put(JSONObject().put("image", image).put("at", at))
            }
            body.put("keyframes", keyframes)
        }
        // Only the chosen workflow's: one picked for another stays picked, but isn't sent.
        val offered = clipLoraChoices()
        val loras = JSONArray()
        for ((name, typed) in clipLoras) {
            if (name !in offered) continue
            val lora = JSONObject().put("name", name)
            typed.trim().takeIf { it.isNotEmpty() }?.let {
                val strength = it.toDoubleOrNull()
                if (strength == null) {
                    clipError = "strength of $name must be a number"
                    return
                }
                lora.put("strength", strength)
            }
            loras.put(lora)
        }
        if (loras.length() > 0) body.put("loras", loras)
        clipError = null
        clipSheet = null
        clipUri = null
        clipSummary = null
        clipState = "starting"
        io {
            try {
                val started = c.startClip(body)
                val id = started.getInt("id")
                clipId = id
                watchClip(c, id)
            } catch (e: Exception) {
                clipError = e.message
                clipState = null
            }
        }
    }

    /** How many keyframes the chosen clip workflow takes; 3 from a broker that doesn't say. */
    fun clipKeyframeLimit(): Int {
        val clip = info?.optJSONObject("clip") ?: return 0
        val name = clipWorkflow ?: clip.optString("default").takeIf { it.isNotEmpty() } ?: return 3
        return clip.optJSONObject("keyframes")?.optInt(name, 3) ?: 3
    }

    /** Whether the chosen clip workflow can make its picture to a voice. */
    fun clipCanLipSync(): Boolean {
        val clip = info?.optJSONObject("clip") ?: return false
        val name = clipWorkflow ?: clip.optString("default").takeIf { it.isNotEmpty() } ?: return false
        return clip.optJSONObject("lip_sync")?.optBoolean(name, false) ?: false
    }

    /** Whether the den can make a voice from a description. */
    fun canDesignVoices(): Boolean = (voiceInfo()?.optJSONArray("design_languages")?.length() ?: 0) > 0

    /** Make the voice [name] from a description on the den, then read its list again. */
    fun designVoice(name: String, description: String) {
        val c = client ?: return
        voiceNote = "designing $name ..."
        io {
            try {
                val request = JSONObject().put("name", name).put("description", description).put("replace", true)
                val result = c.designVoice(request) { msg -> voiceNote = DenClient.describeProgress(msg) }
                clipVoice = name
                voiceNote = "voice $name designed (${result.optDouble("duration")} s sample)"
                loadStatus(c)
            } catch (e: Exception) {
                voiceNote = e.message
            }
        }
    }

    /** Record the narration; a second call stops, and the den writes down what was said as the timed script. */
    fun toggleNarration() {
        val c = client ?: return
        if (!recorder.recording) {
            try {
                recorder.start()
                recording = true
                recordingNarration = true
                voiceNote = "recording the narration: speak the lines, then tap Stop"
            } catch (e: Exception) {
                voiceNote = "can't record: ${e.message}"
            }
            return
        }
        recording = false
        recordingNarration = false
        val (file, _) = recorder.stop() ?: run { voiceNote = "nothing was recorded"; return }
        val picked = Picked(file.name, file.readBytes())
        file.delete()
        voiceNote = "writing down what you said ..."
        io {
            try {
                val audio = JSONObject().put("name", picked.name).put("base64", Base64.encodeToString(picked.bytes, Base64.NO_WRAP))
                val request = JSONObject().put("audio", audio)
                clipLanguage?.let { request.put("language", it) }
                val result = c.transcribe(request) { msg -> voiceNote = DenClient.describeProgress(msg) }
                clipVoiceover = result.getString("srt")
                narration = picked
                useNarration = true
                voiceNote = "heard ${result.optJSONArray("segments")?.length() ?: 0} line(s); your recording is the voice-over"
            } catch (e: Exception) {
                voiceNote = e.message
            }
        }
    }

    /** Record a voice sample; a second call stops and keeps it as the voice [name]. */
    fun toggleRecording(name: String) {
        if (!recorder.recording) {
            try {
                recorder.start()
                recording = true
                voiceNote = "recording: speak naturally for about 10 seconds, then tap Stop"
            } catch (e: Exception) {
                voiceNote = "can't record: ${e.message}"
            }
            return
        }
        recording = false
        val (file, seconds) = recorder.stop() ?: run { voiceNote = "nothing was recorded"; return }
        if (seconds < 4) {
            file.delete()
            voiceNote = "that was ${"%.1f".format(seconds)} s; record 5–15 seconds of speech"
            return
        }
        addVoice(name, Picked(file.name, file.readBytes()))
        file.delete()
    }

    /** Keep the last clip's timed script as a file in Download/den. */
    fun saveSrt() {
        val srt = lastSrt ?: return
        val name = "den-" + SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(Date()) + ".srt"
        val resolver = getApplication<Application>().contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.MIME_TYPE, "application/x-subrip")
            put(MediaStore.Downloads.RELATIVE_PATH, "Download/den")
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
        if (uri == null) {
            voiceNote = "could not save the script"
            return
        }
        resolver.openOutputStream(uri)!!.use { it.write(srt.toByteArray()) }
        voiceNote = "script saved to Download/den/$name"
    }

    /** The broker's voices and languages, or null where speech isn't installed (ADR 0010). */
    fun voiceInfo(): JSONObject? = info?.optJSONObject("voice")

    fun voiceNames(): List<String> =
        voiceInfo()?.optJSONArray("voices")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()

    fun languages(): List<String> = voiceInfo()?.optJSONObject("languages")?.keys()?.asSequence()?.sorted()?.toList().orEmpty()

    /** An SRT file's script into the voice-over field. */
    fun loadScript(picked: Picked) {
        clipVoiceover = picked.bytes.toString(Charsets.UTF_8).removePrefix("\uFEFF")
    }

    /** Keep a recording as a voice on the den, then read its list again. */
    fun addVoice(name: String, picked: Picked) {
        val c = client ?: return
        voiceNote = "adding voice $name ..."
        io {
            try {
                val recording = JSONObject().put("name", picked.name)
                    .put("base64", Base64.encodeToString(picked.bytes, Base64.NO_WRAP))
                c.addVoice(JSONObject().put("name", name).put("recording", recording).put("replace", true))
                clipVoice = name
                voiceNote = "voice $name kept"
                loadStatus(c)
            } catch (e: Exception) {
                voiceNote = e.message
            }
        }
    }

    /** Whether the chosen clip workflow makes sound; false from a broker that doesn't say. */
    fun clipMakesSound(): Boolean {
        val clip = info?.optJSONObject("clip") ?: return false
        val name = clipWorkflow ?: clip.optString("default").takeIf { it.isNotEmpty() } ?: return false
        return clip.optJSONObject("sound")?.optBoolean(name, false) ?: false
    }

    /** {name: (description, default strength)} of the LoRAs the chosen clip workflow takes. */
    fun clipLoraChoices(): Map<String, Pair<String, Double>> {
        val clip = info?.optJSONObject("clip") ?: return emptyMap()
        val name = clipWorkflow ?: clip.optString("default").takeIf { it.isNotEmpty() } ?: return emptyMap()
        val loras = clip.optJSONObject("loras")?.optJSONObject(name) ?: return emptyMap()
        return loras.keys().asSequence().sorted().associateWith { lora ->
            val spec = loras.getJSONObject(lora)
            spec.optString("description") to spec.optDouble("strength", 1.0)
        }
    }

    /** A new keyframe goes first at the start, last at the end, and between them at the middle. */
    fun addClipKeyframe(picked: Picked) {
        val at = when (clipKeyframes.size) {
            0 -> "start"
            1 -> "end"
            else -> "50%"
        }
        clipKeyframes.add(ClipKeyframe(picked, at))
    }

    fun cancelClip() {
        val c = client ?: return
        val id = clipId ?: return
        io {
            try {
                c.cancelClip(id)
            } catch (e: Exception) {
                clipError = e.message
            }
        }
    }

    /** Ask how the clip goes every few seconds until it's done; a lost connection only pauses it. */
    private suspend fun watchClip(c: DenClient, id: Int) {
        if (watchingClip) return
        watchingClip = true
        try {
            while (clipId == id) {
                val view = c.clip(id)
                when (view.getString("state")) {
                    "done" -> {
                        finishClip(c, id)
                        return
                    }
                    "failed", "cancelled" -> {
                        clipError = "clip ${view.getString("state")}: ${view.optString("error")}"
                        clipState = null
                        clipId = null
                        return
                    }
                    "running" -> clipState = "making the clip: ${view.optInt("running_s")} s" + timeLeft(view)
                    else -> clipState = view.optJSONObject("progress")?.let { DenClient.describeProgress(it) } ?: "waiting for the image side"
                }
                delay(3000)
            }
        } catch (e: Exception) {
            clipError = "${e.message} (the clip goes on; it is picked up again when the app comes back)"
        } finally {
            watchingClip = false
        }
    }

    private fun timeLeft(view: JSONObject): String = when {
        !view.has("left_s") -> ""
        view.isNull("left_s") -> ", time left unknown"
        view.getInt("left_s") < 60 -> ", under a minute left"
        else -> ", about ${Math.round(view.getInt("left_s") / 60.0)} min left"
    }

    private fun finishClip(c: DenClient, id: Int) {
        val result = c.clip(id, bytes = true).getJSONObject("result")
        val video = Base64.decode(result.getString("clip"), Base64.DEFAULT)
        val (uri, where) = saveVideo(video, result.optString("suffix", ".mp4"))
        if (!result.isNull("sheet")) {
            val sheet = Base64.decode(result.getString("sheet"), Base64.DEFAULT)
            clipSheet = BitmapFactory.decodeByteArray(sheet, 0, sheet.size)
        }
        val summary = result.optJSONArray("summary")?.let { a -> (0 until a.length()).joinToString(" · ") { a.getString(it) } }
        val notes = result.optJSONArray("notes")?.let { a -> (0 until a.length()).map { "note: ${a.getString(it)}" } }.orEmpty()
        lastSrt = result.optString("srt").takeIf { it.isNotEmpty() }
        clipSummary = (listOfNotNull(summary, "${result.opt("seconds")} s", "saved to $where") + notes).joinToString("\n")
        clipUri = uri
        clipState = null
        clipId = null
    }

    private fun saveVideo(data: ByteArray, suffix: String): Pair<Uri, String> {
        val name = "den-" + SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.ROOT).format(Date()) + suffix
        val resolver = getApplication<Application>().contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, name)
            put(MediaStore.Video.Media.MIME_TYPE, if (suffix == ".webm") "video/webm" else "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH, "Movies/den")
            put(MediaStore.Video.Media.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw DenException("could not save the clip to the gallery")
        resolver.openOutputStream(uri)!!.use { it.write(data) }
        values.clear()
        values.put(MediaStore.Video.Media.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return uri to "Movies/den/$name"
    }

    private fun saveToGallery(png: ByteArray): String {
        val name = "den-" + SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.ROOT).format(Date()) + ".png"
        val resolver = getApplication<Application>().contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, name)
            put(MediaStore.Images.Media.MIME_TYPE, "image/png")
            put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/den")
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw DenException("could not save the image to the gallery")
        resolver.openOutputStream(uri)!!.use { it.write(png) }
        values.clear()
        values.put(MediaStore.Images.Media.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return "Pictures/den/$name"
    }

    // --- poses ---

    fun loadPoses() {
        val c = client ?: return
        loadingPoses = true
        io {
            try {
                val p = c.poses()
                poseToc = p.optString("toc")
                val names = p.optJSONArray("names")
                poseNames = (0 until (names?.length() ?: 0)).map { names!!.getString(it) }
                posesError = null
            } catch (e: Exception) {
                posesError = e.message
            } finally {
                loadingPoses = false
            }
        }
    }

    fun openPose(name: String?) {
        pose = name
        poseDetails = null
        poseImages = emptyList()
        val c = client ?: return
        if (name == null) return
        io {
            try {
                val p = c.pose(name)
                poseDetails = p.optString("details")
                val images = p.optJSONArray("images")
                poseImages = (0 until (images?.length() ?: 0)).mapNotNull { i ->
                    val b = Base64.decode(images!!.getJSONObject(i).getString("base64"), Base64.DEFAULT)
                    BitmapFactory.decodeByteArray(b, 0, b.size)
                }
            } catch (e: Exception) {
                posesError = e.message
            }
        }
    }

    // --- files picked on this device ---

    fun readPicked(uri: Uri): Picked? {
        val resolver = getApplication<Application>().contentResolver
        val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) it.getString(0) else null
        } ?: uri.lastPathSegment ?: "file"
        val bytes = resolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
        // Only the name: a path of this device never goes to the broker.
        return Picked(name.substringAfterLast('/'), bytes)
    }

    // --- chat ---

    fun loadConversations() = io { conversations = chats.list() }

    fun newChat() {
        chat = chats.create()
        chatError = null
        chatTick++
    }

    fun openChat(conversation: Conversation) {
        chat = conversation
        chatError = null
        // A turn that failed earlier may have left an answer with nothing in it.
        val before = conversation.messages.size
        dropEmptyAnswer(conversation)
        if (conversation.messages.size != before) io { chats.save(conversation) }
        chatTick++
    }

    fun closeChat() {
        chat = null
        loadConversations()
    }

    fun renameChat(title: String) {
        val conversation = chat ?: return
        conversation.title = title.ifBlank { conversation.title }
        io {
            chats.save(conversation)
            conversations = chats.list()
        }
        chatTick++
    }

    fun deleteChat(conversation: Conversation, withImages: Boolean) = io {
        chats.delete(conversation, withImages)
        if (chat?.id == conversation.id) chat = null
        conversations = chats.list()
    }

    fun galleryCount(conversation: Conversation): Int = runCatching { chats.galleryCount(conversation) }.getOrDefault(0)

    fun imageOf(conversation: Conversation, name: String): Bitmap? =
        bitmaps.getOrPut("${conversation.id}/$name") {
            BitmapFactory.decodeFile(chats.imageFile(conversation, name).path)
        }

    private val bitmaps = mutableMapOf<String, Bitmap?>()

    /**
     * What skills the broker's den has. Only asked for when the user opens the picker, and only
     * what the user then picks is ever loaded; a den that doesn't serve them leaves it empty.
     */
    fun loadSkills() {
        val c = client ?: return
        if (skillsLoading) return
        skillsLoading = true
        io {
            try {
                val list = c.skills()
                skillList = (0 until list.length()).map { list.getJSONObject(it) }
                skillsError = null
            } catch (e: Exception) {
                skillList = emptyList()
                skillsError = e.message
            } finally {
                skillsLoading = false
            }
        }
    }

    /**
     * What models the broker's machine has. Asked for when the picker opens; it loads nothing,
     * but choosing another model does cost a swap there, which the picker says.
     */
    fun loadModels() {
        val c = client ?: return
        if (modelsLoading) return
        modelsLoading = true
        io {
            models = runCatching { c.models() }.getOrDefault(emptyList())
            modelsLoading = false
        }
    }

    /** The model this conversation asks for; null goes back to the den's own selected one. */
    fun useModel(name: String?) {
        val conversation = chat ?: return
        conversation.model = name
        chatError = null
        io { chats.save(conversation) }
        chatTick++
    }

    /** What the den has selected, which a conversation uses unless it asks for another. */
    fun defaultModel(): String? = status?.textOrNull("llm_model")

    /** Load one skill (or one of its references) into this conversation's system prompt. */
    fun attachSkill(name: String, reference: String? = null) {
        val c = client ?: return
        val conversation = chat ?: return
        if (conversation.attachments.any { it.name == name && it.reference == reference }) return
        io {
            try {
                val skill = c.skill(name, reference)
                conversation.attachments.add(Attachment(name, reference, skill.text("text")))
                chats.save(conversation)
                skillsError = null
            } catch (e: Exception) {
                skillsError = e.message
            }
            chatTick++
        }
    }

    fun detachSkill(attachment: Attachment) {
        val conversation = chat ?: return
        conversation.attachments.remove(attachment)
        io { chats.save(conversation) }
        chatTick++
    }

    fun toggleThinking(on: Boolean) {
        thinking = on
        settings.thinking = on
    }

    fun toggleStreamWithTools(on: Boolean) {
        streamWithTools = on
        settings.streamWithTools = on
    }

    /** Reads a picked or photographed image, scales it down and holds it for the next message. */
    fun attachImage(uri: android.net.Uri, name: String = "image") = io {
        val attached = Media.attach(getApplication(), uri, name)
        if (attached == null) chatError = "that file isn't an image this phone can read"
        else pending.add(attached)
    }

    fun removeAttachment(attached: Attached) {
        pending.remove(attached)
    }

    /** Where the camera writes the photo it takes; app-private, handed over as a content URI. */
    fun cameraTarget(): android.net.Uri {
        val folder = java.io.File(getApplication<Application>().cacheDir, "camera").apply { mkdirs() }
        return Media.share(getApplication(), java.io.File(folder, "photo-${System.currentTimeMillis()}.jpg"))
    }

    /** Sends the draft and runs the model's turn, including any image it asks for. */
    fun send() {
        val c = client ?: return
        val conversation = chat ?: return
        val text = draft.trim()
        if ((text.isEmpty() && pending.isEmpty()) || chatBusy) return
        draft = ""
        chatError = null
        if (conversation.messages.none { it.role == "user" }) conversation.title = title(text.ifEmpty { "Image" })
        val sent = ChatMessage("user", text)
        // The scaled copies stay with the conversation, so the history still shows them later.
        pending.forEach { sent.pictures.add(chats.saveAttachment(conversation, it)) }
        if (pending.isNotEmpty()) {
            sent.note = sent.pictures.zip(pending).joinToString(", ") { (picture, attached) ->
                "${picture.id}: ${attached.width}x${attached.height}" + if (attached.scaled) " (scaled)" else ""
            }
        }
        pending.clear()
        conversation.messages.add(sent)
        chatTick++
        startTurn(c, conversation)
    }

    /** Throw the last answer away and let the model try again from the same history. */
    fun retry() {
        val c = client ?: return
        val conversation = chat ?: return
        if (chatBusy || !History.canRetry(conversation.messages)) return
        replace(conversation, History.withoutLastAnswer(conversation.messages))
        chatError = null
        startTurn(c, conversation)
    }

    /**
     * Stop the running turn: the answer so far stays, marked as stopped, and a picture being
     * made is cancelled. The broker stops the model as soon as the channel closes.
     */
    fun stopTurn() {
        turnStop?.stop()
    }

    /** Runs the model's turn in the background, with a Stop of its own. */
    private fun startTurn(c: DenClient, conversation: Conversation) {
        val stop = Stop()
        turnStop = stop
        chatBusy = true
        io {
            try {
                runTurn(c, conversation, stop)
            } catch (e: Exception) {
                chatError = e.message ?: e.javaClass.simpleName
            } finally {
                turnStop = null
                chatBusy = false
                dropEmptyAnswer(conversation)
                chats.save(conversation)
                conversations = chats.list()
                chatTick++
            }
        }
    }

    /** Take the last message sent, and everything after it, back into the input box. */
    fun resend() {
        val conversation = chat ?: return
        if (chatBusy || !History.canResend(conversation.messages)) return
        val (kept, sent) = History.withoutLastSent(conversation.messages)
        replace(conversation, kept)
        chatError = null
        draft = sent?.content.orEmpty()
        pending.clear()
        sent?.images?.forEach { name ->
            chats.bytesOf(conversation, name)?.let { Media.scale(it, name) }?.let { pending.add(it) }
        }
        chatTick++
    }

    /** The conversation's messages, replaced and stored; the image files themselves stay. */
    private fun replace(conversation: Conversation, messages: List<ChatMessage>) {
        val kept = messages.toList()
        conversation.messages.clear()
        conversation.messages.addAll(kept)
        chats.save(conversation)
        chatTick++
    }

    /** A turn that failed leaves an answer with nothing in it; it isn't worth a bubble. */
    private fun dropEmptyAnswer(conversation: Conversation) {
        conversation.messages.removeAll {
            it.role == "assistant" && it.content.isBlank() && it.thinking.isBlank() &&
                it.toolCalls == null && it.images.isEmpty()
        }
    }

    private fun title(text: String): String =
        text.lineSequence().first().trim().take(48).ifEmpty { "New chat" }

    /** The model answers; if it calls the image tool, run it and let it answer again. */
    private fun runTurn(c: DenClient, conversation: Conversation, stop: Stop) {
        for (round in 1..MAX_TOOL_ROUNDS) {
            if (stop.stopped) return
            val body = chatBody(conversation)
            val assistant = ChatMessage("assistant")
            conversation.messages.add(assistant)
            chatTick++
            val carriedPictures = ChatRequest.carriesPictures(body)
            val written = StringBuilder()
            val thought = StringBuilder()
            val turn = try {
                c.chat(body, stop) { chunk ->
                val delta = chunk.optJSONArray("choices")?.optJSONObject(0)?.optJSONObject("delta") ?: return@chat
                delta.opt("content")?.takeIf { it != JSONObject.NULL }?.let {
                    written.append(it.toString())
                    assistant.content = written.toString()
                }
                delta.opt("reasoning_content")?.takeIf { it != JSONObject.NULL }?.let {
                    thought.append(it.toString())
                    assistant.thinking = thought.toString()
                }
                    if (delta.optJSONArray("tool_calls") != null) assistant.note = "calling ${ImageTool.NAME}"
                    chatTick++
                }
            } catch (e: DenException) {
                throw DenException(Failures.chat(e.message, carriedPictures))
            } catch (e: StoppedException) {
                // What it wrote and thought so far stays; any tool call it was writing doesn't run.
                assistant.content = written.toString()
                assistant.thinking = thought.toString()
                assistant.note = "stopped"
                chatTick++
                return
            }
            assistant.content = turn.content
            assistant.thinking = turn.thinking
            assistant.note = null
            val calls = turn.toolCalls().filter { it.name.isNotEmpty() || it.arguments.isNotBlank() }
            chatTick++
            if (calls.isEmpty()) return
            assistant.toolCalls = JSONArray(
                calls.map { call ->
                    JSONObject().put("id", call.id).put("type", "function")
                        .put("function", JSONObject().put("name", call.name).put("arguments", call.arguments))
                }
            )
            chats.save(conversation)
            // Every call of this answer runs before the model is asked again: one swap, not one
            // per picture.
            calls.forEachIndexed { at, call -> runToolCall(c, conversation, call, stop, at + 1, calls.size) }
        }
        chatError = "the model kept calling tools; stopped after $MAX_TOOL_ROUNDS rounds"
    }

    /** One tool call: check it against this den, run it, and answer the model with the result. */
    private fun runToolCall(c: DenClient, conversation: Conversation, call: ToolCall, stop: Stop, of: Int = 1, calls: Int = 1) {
        val message = ChatMessage("tool", toolCallId = call.id, toolName = call.name)
        conversation.messages.add(message)
        chatTick++
        val done = mutableListOf<String>()
        message.content = try {
            // Every call still gets its answer, so the history stays one the model can take.
            stop.check()
            if (call.name != ImageTool.NAME) throw ToolArgumentException("there is no tool called ${call.name}")
            val spec = info ?: throw ToolArgumentException("this den's tools are unknown; reconnect")
            val request = ImageTool.request(spec, call.arguments, conversation.pictures().map { it.id })
            // A call names the pictures it edits by id; with none named it makes a new one.
            val targets = ImageTool.targets(request, lastMessagePictures(conversation).map { it.id })
            request.remove(ImageTool.WHICH)
            val runs = targets.ifEmpty { listOf("") }
            runs.forEachIndexed { step, editId ->
                // Say where we are, whether the turn is one call per picture or one call for all.
                val step_of = when {
                    runs.size > 1 -> "${step + 1} of ${runs.size} · "
                    calls > 1 -> "$of of $calls · "
                    else -> ""
                }
                val one = JSONObject(request.toString())
                if (editId.isNotEmpty()) {
                    val picture = conversation.picture(editId)
                        ?: throw ToolArgumentException("no picture $editId in this conversation")
                    val bytes = chats.bytesOf(conversation, picture.file)
                        ?: throw ToolArgumentException("the file of $editId is gone from this phone")
                    one.put("image", JSONObject().put("name", picture.file).put("base64", Base64.encodeToString(bytes, Base64.NO_WRAP)))
                    one.put("workflow", ImageTool.editWorkflow(spec, one.optString("workflow").takeIf { it.isNotEmpty() }))
                }
                message.note = step_of + "starting"
                chatTick++
                val result = c.image(one, stop) {
                    message.note = step_of + DenClient.describeProgress(it)
                    chatTick++
                }
                message.note = null
                val images = result.optJSONArray("images")
                val saved = (0 until (images?.length() ?: 0)).map {
                    chats.saveImage(conversation, Base64.decode(images!!.getString(it), Base64.DEFAULT))
                }
                saved.forEach { message.pictures.add(it.first) }
                val summary = result.optJSONArray("summary")?.let { a -> (0 until a.length()).joinToString(", ") { a.getString(it) } }
                val made = saved.joinToString(", ") { it.first.id }
                val what = if (editId.isNotEmpty()) "edited $editId → $made" else "made $made"
                done += "$what: $summary, ${result.opt("seconds")} s, saved to ${saved.joinToString { it.second }}"
                chats.save(conversation)
                chatTick++
            }
            "Done. " + done.joinToString("; ") + ". Shown to the user."
        } catch (e: ToolArgumentException) {
            "Error: ${e.message}"
        } catch (e: DenException) {
            "Error: ${e.message}"
        } catch (e: StoppedException) {
            "Stopped by the user" + if (done.isEmpty()) "." else " after: " + done.joinToString("; ") + "."
        } finally {
            message.note = null
        }
        chats.save(conversation)
        chatTick++
    }

    /** The pictures of the newest message that carried any — what "all" means to the tool. */
    private fun lastMessagePictures(conversation: Conversation): List<Picture> =
        conversation.messages.lastOrNull { it.role == "user" && it.pictures.isNotEmpty() }?.pictures.orEmpty()

    /** The request body: the conversation trimmed to the context window, plus the image tool. */
    private fun chatBody(conversation: Conversation): JSONObject {
        val tools = info?.let { ImageTool.tools(it, ids = conversation.pictures().map { picture -> picture.id }) }
        val instructions = StringBuilder(
            "You are den, a local assistant on the user's own machine, answering from an Android app. " +
                "Keep answers short." +
                if (tools != null) {
                    " You can generate images with the generate_image tool; the user sees them in the chat. " +
                        "Every picture in this conversation has an id, shown to you in square brackets before it " +
                        "and to the user next to it: refer to a picture only by its id, never by its position or " +
                        "by \"the last one\". A picture from an earlier turn can be edited by its id as well. " +
                        "Every call you make in one answer runs together before you are asked again, so when the " +
                        "same thing applies to several pictures, make one call per picture in that same answer. " +
                        "One call per answer instead makes the den swap its GPU between the language model and " +
                        "the image model each time, which costs tens of seconds every round."
                } else "",
        )
        // Skills the user loaded into this conversation, marked as den's own instructions.
        for (attachment in conversation.attachments) {
            instructions.append("\n\n## Instructions from den — skill ").append(attachment.label).append("\n\n")
            instructions.append(attachment.text)
        }
        return ChatRequest.build(
            // A conversation may ask for another model; the broker loads it, which takes a while.
            model = conversation.model ?: status?.textOrNull("llm_model").orEmpty(),
            numCtx = status?.optInt("num_ctx") ?: 0,
            systemPrompt = instructions.toString(),
            history = conversation.messages.toList(),
            pictures = { message ->
                // Each picture goes with the id it is known by here, so the model can name it.
                message.pictures.mapNotNull { picture ->
                    chats.bytesOf(conversation, picture.file)?.let { picture.id to Media.dataUrl(it) }
                }
            },
            tools = tools,
            stream = tools == null || streamWithTools,
            thinking = thinking,
        )
    }

    private fun io(block: suspend () -> Unit) {
        viewModelScope.launch { withContext(Dispatchers.IO) { block() } }
    }

    override fun onCleared() {
        tunnel?.close()
    }

    private companion object {
        const val MAX_TOOL_ROUNDS = 6
    }

}
