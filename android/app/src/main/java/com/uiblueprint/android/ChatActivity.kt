package com.uiblueprint.android

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import android.view.Gravity
import android.view.View
import android.widget.EditText
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.recyclerview.widget.LinearLayoutManager
import com.uiblueprint.android.databinding.ActivityChatBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * Global chat screen.
 *
 * Features
 * --------
 * - Messages displayed in a RecyclerView using [ChatMessageAdapter].
 * - Always-visible Copy / Share action row under each message.
 * - Edit button on user messages: opens a dialog, sends an edit request to the
 *   backend (POST /api/chat/{id}/edit), and refreshes the conversation.
 * - Long-press enters multi-select mode; toolbar shows Select All / Copy / Share / Cancel.
 * - Agent Mode toggle: persisted in SharedPreferences.
 *   When enabled, sends ``X-Agent-Mode: 1`` header + ``agent_mode: true`` body
 *   so the backend formats the response with ARTIFACT_* sections.
 * - ARTIFACT_* blocks are rendered as a monospace card with their own Copy button.
 *
 * Authorization: Bearer <BACKEND_API_KEY> is added when the key is non-empty.
 */
class ChatActivity : AppCompatActivity(), ChatMessageAdapter.MessageActionListener {

    private lateinit var binding: ActivityChatBinding
    private lateinit var prefs: SharedPreferences
    private val executor = Executors.newSingleThreadExecutor { Thread(it, "ChatActivity-worker") }
    private lateinit var adapter: ChatMessageAdapter
    private lateinit var fileAdapter: ChatFileAdapter

    // GLOBAL_CONVERSATION_ACTIVATION_V1: single active conversation per session.
    private var conversationId: String? = null
    private val chatFiles = mutableListOf<ChatFile>()

    private val speechInputLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val matches = result.data
                ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            if (!matches.isNullOrEmpty()) {
                val current = binding.etMessage.text.toString()
                binding.etMessage.setText(
                    if (current.isBlank()) matches[0] else "$current ${matches[0]}"
                )
                binding.etMessage.setSelection(binding.etMessage.text.length)
            }
        }
    }

    private val micPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startSpeechRecognition()
        } else {
            Toast.makeText(
                this,
                getString(R.string.toast_mic_permission_denied),
                Toast.LENGTH_SHORT,
            ).show()
        }
    }

    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        uri?.let { uploadFile(it) }
    }

    companion object {
        private const val PREFS_NAME = "chat_prefs"
        private const val PREF_AGENT_MODE = "agent_mode"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        adapter = ChatMessageAdapter(this)
        binding.rvMessages.layoutManager = LinearLayoutManager(this).apply {
            stackFromEnd = true
        }
        binding.rvMessages.adapter = adapter

        // Restore agent mode preference.
        binding.switchAgentMode.isChecked = prefs.getBoolean(PREF_AGENT_MODE, false)
        binding.switchAgentMode.setOnCheckedChangeListener { _, isChecked ->
            prefs.edit().putBoolean(PREF_AGENT_MODE, isChecked).apply()
        }

        binding.btnSend.setOnClickListener { onSendClicked() }

        // Attach button now triggers file upload
        binding.btnAttach.setOnClickListener {
            filePickerLauncher.launch("*/*")
        }

        // New conversation button in toolbar
        binding.btnNewChat.setOnClickListener {
            conversationId = null
            adapter.submitList(emptyList())
            Toast.makeText(this, "Started new conversation", Toast.LENGTH_SHORT).show()
        }

        setupMicButton()

        // Multi-select toolbar buttons
        binding.btnSelectAll.setOnClickListener {
            adapter.selectAll()
        }
        binding.btnCopySelected.setOnClickListener {
            val text = adapter.getSelectedMessages().joinToString("\n\n") {
                "${if (it.role == "user") "You" else "AI"}: ${it.content}"
            }
            copyToClipboard(text)
            adapter.clearSelection()
            updateMultiSelectToolbar()
            Toast.makeText(this, getString(R.string.toast_copied), Toast.LENGTH_SHORT).show()
        }
        binding.btnShareSelected.setOnClickListener {
            val text = adapter.getSelectedMessages().joinToString("\n\n") {
                "${if (it.role == "user") "You" else "AI"}: ${it.content}"
            }
            shareText(text)
            adapter.clearSelection()
            updateMultiSelectToolbar()
        }
        binding.btnCancelSelect.setOnClickListener {
            adapter.clearSelection()
            updateMultiSelectToolbar()
        }

        setupFilePanel()
    }

    private fun setupFilePanel() {
        // Initialize file adapter
        fileAdapter = ChatFileAdapter(object : ChatFileAdapter.FileActionListener {
            override fun onToggleIncludeInContext(file: ChatFile, included: Boolean) {
                updateFileContext(file, included)
            }

            override fun onRenameFile(file: ChatFile) {
                showRenameFileDialog(file)
            }

            override fun onDeleteFile(file: ChatFile) {
                showDeleteFileDialog(file)
            }

            override fun onDownloadFile(file: ChatFile) {
                file.downloadUrl?.let { url ->
                    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    startActivity(intent)
                }
            }
        })

        // Set drawer width to 40% of screen width
        val params = binding.filesPanel.root.layoutParams
        params.width = (resources.displayMetrics.widthPixels * 0.4).toInt()
        binding.filesPanel.root.layoutParams = params

        // Setup RecyclerView
        val rvFiles = binding.filesPanel.rvFiles
        val btnUploadFile = binding.filesPanel.btnUploadFile
        val btnBrowseAllFiles = binding.filesPanel.btnBrowseAllFiles
        val btnGithubRepos = binding.filesPanel.btnGithubRepos
        val btnClosePanel = binding.filesPanel.btnClosePanel

        rvFiles.layoutManager = LinearLayoutManager(this)
        rvFiles.adapter = fileAdapter

        // Hamburger menu to open drawer
        binding.btnFilesMenu.setOnClickListener {
            binding.drawerLayout.openDrawer(GravityCompat.END)
        }

        // Resource menu button
        binding.btnResourceMenu.setOnClickListener {
            ResourceActivity.start(this, conversationId)
        }

        // Close panel button
        btnClosePanel.setOnClickListener {
            binding.drawerLayout.closeDrawer(GravityCompat.END)
        }

        // Upload file button
        btnUploadFile.setOnClickListener {
            filePickerLauncher.launch("*/*")
        }

        // Browse all files button
        btnBrowseAllFiles.setOnClickListener {
            showAllFilesDialog()
        }

        // GitHub repos button (placeholder for now)
        btnGithubRepos.setOnClickListener {
            showGithubRepoDialog()
        }

        // Update empty state visibility
        updateFileListUI()
    }

    override fun onResume() {
        super.onResume()
        loadMessages()
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
    }

    // -------------------------------------------------------------------------
    // ChatMessageAdapter.MessageActionListener
    // -------------------------------------------------------------------------

    override fun onCopyMessage(message: ChatMessageAdapter.Message) {
        copyToClipboard(message.content)
        Toast.makeText(this, getString(R.string.toast_copied), Toast.LENGTH_SHORT).show()
    }

    override fun onShareMessage(message: ChatMessageAdapter.Message) {
        shareText(message.content)
    }

    override fun onEditMessage(message: ChatMessageAdapter.Message) {
        showEditDialog(message)
    }

    override fun onSelectionChanged(selectedCount: Int) {
        updateMultiSelectToolbar()
    }

    // -------------------------------------------------------------------------
    // File Management
    // -------------------------------------------------------------------------

    private fun loadChatFiles() {
        val convId = conversationId ?: return
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/$convId/files")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .get()
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "[]"
                    val filesArray = JSONArray(body)
                    val files = mutableListOf<ChatFile>()

                    for (i in 0 until filesArray.length()) {
                        val obj = filesArray.getJSONObject(i)
                        val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
                        files.add(
                            ChatFile(
                                id = obj.getString("id"),
                                conversationId = obj.getString("conversation_id"),
                                filename = obj.getString("filename"),
                                mimeType = obj.getString("mime_type"),
                                sizeBytes = obj.getLong("size_bytes"),
                                category = obj.getString("category"),
                                includedInContext = obj.getBoolean("included_in_context"),
                                createdAt = dateFormat.parse(obj.getString("created_at").split(".")[0]) ?: Date(),
                                updatedAt = dateFormat.parse(obj.getString("updated_at").split(".")[0]) ?: Date(),
                                downloadUrl = obj.optString("download_url", null),
                            )
                        )
                    }

                    runOnUiThread {
                        chatFiles.clear()
                        chatFiles.addAll(files)
                        fileAdapter.submitList(chatFiles)
                        updateFileListUI()
                    }
                } else {
                    Log.e("ChatActivity", "Failed to load files: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error loading files", e)
            }
        }
    }

    private fun uploadFile(uri: Uri) {
        val convId = conversationId ?: run {
            Toast.makeText(this, "Please start a conversation first", Toast.LENGTH_SHORT).show()
            return
        }

        executor.execute {
            try {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.status_uploading_file), Toast.LENGTH_SHORT).show()
                }

                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                // Use the chunked upload helper
                val success = ChatFileUploadHelper.uploadFile(
                    uri = uri,
                    conversationId = convId,
                    apiKey = apiKey,
                    baseUrl = baseUrl,
                    contentResolver = contentResolver,
                    cacheDir = cacheDir,
                    onProgress = { current, total ->
                        runOnUiThread {
                            if (total > 1) {
                                Toast.makeText(
                                    this,
                                    "Uploading… chunk $current/$total",
                                    Toast.LENGTH_SHORT
                                ).show()
                            }
                        }
                    }
                )

                if (success) {
                    runOnUiThread {
                        Toast.makeText(this, getString(R.string.status_file_uploaded), Toast.LENGTH_SHORT).show()
                        loadChatFiles()
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(
                            this,
                            getString(R.string.error_file_upload_failed),
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error uploading file", e)
                runOnUiThread {
                    val errorMsg = when {
                        e.message?.contains("failed to connect") == true -> 
                            getString(R.string.error_backend_connection_failed)
                        e.message?.contains("timeout") == true -> 
                            getString(R.string.error_backend_timeout)
                        else -> getString(R.string.error_file_upload_failed)
                    }
                    Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun updateFileContext(file: ChatFile, included: Boolean) {
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                val json = JSONObject().apply {
                    put("included_in_context", included)
                }

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/${file.conversationId}/files/${file.id}")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .addHeader("Content-Type", "application/json")
                    .patch(json.toString().toRequestBody("application/json".toMediaType()))
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    runOnUiThread {
                        // Update local list
                        val index = chatFiles.indexOfFirst { it.id == file.id }
                        if (index >= 0) {
                            chatFiles[index] = file.copy(includedInContext = included)
                            fileAdapter.submitList(chatFiles.toList())
                        }
                    }
                } else {
                    Log.e("ChatActivity", "Failed to update file context: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error updating file context", e)
            }
        }
    }

    private fun showRenameFileDialog(file: ChatFile) {
        val input = EditText(this).apply {
            setText(file.filename)
            setSelection(text.length)
        }

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_rename_file_title_chat))
            .setView(input)
            .setPositiveButton(getString(R.string.action_ok)) { _, _ ->
                val newName = input.text.toString().trim()
                if (newName.isNotEmpty() && newName != file.filename) {
                    renameFile(file, newName)
                }
            }
            .setNegativeButton(getString(R.string.action_cancel), null)
            .show()
    }

    private fun renameFile(file: ChatFile, newName: String) {
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                val json = JSONObject().apply {
                    put("filename", newName)
                }

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/${file.conversationId}/files/${file.id}")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .addHeader("Content-Type", "application/json")
                    .patch(json.toString().toRequestBody("application/json".toMediaType()))
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    runOnUiThread {
                        loadChatFiles()
                        Toast.makeText(this, "File renamed", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    Log.e("ChatActivity", "Failed to rename file: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error renaming file", e)
            }
        }
    }

    private fun showDeleteFileDialog(file: ChatFile) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_delete_file_title_chat))
            .setMessage(getString(R.string.dialog_delete_file_message_chat, file.filename))
            .setPositiveButton(getString(R.string.action_delete)) { _, _ ->
                deleteFile(file)
            }
            .setNegativeButton(getString(R.string.action_cancel), null)
            .show()
    }

    private fun deleteFile(file: ChatFile) {
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/${file.conversationId}/files/${file.id}")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .delete()
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    runOnUiThread {
                        chatFiles.removeIf { it.id == file.id }
                        fileAdapter.submitList(chatFiles.toList())
                        updateFileListUI()
                        Toast.makeText(this, "File deleted", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    Log.e("ChatActivity", "Failed to delete file: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error deleting file", e)
            }
        }
    }

    private fun updateFileListUI() {
        val tvEmptyState = binding.filesPanel.tvEmptyState
        val rvFiles = binding.filesPanel.rvFiles

        if (chatFiles.isEmpty()) {
            tvEmptyState.visibility = View.VISIBLE
            rvFiles.visibility = View.GONE
        } else {
            tvEmptyState.visibility = View.GONE
            rvFiles.visibility = View.VISIBLE
        }
    }

    private fun showAllFilesDialog() {
        // Load all files from all conversations
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                // Use dummy conversation_id since we're fetching all files
                val request = Request.Builder()
                    .url("$baseUrl/api/chat/_all/files?all_conversations=true")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .get()
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "[]"
                    val filesArray = JSONArray(body)
                    val allFiles = mutableListOf<ChatFile>()

                    for (i in 0 until filesArray.length()) {
                        val obj = filesArray.getJSONObject(i)
                        val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
                        allFiles.add(
                            ChatFile(
                                id = obj.getString("id"),
                                conversationId = obj.getString("conversation_id"),
                                filename = obj.getString("filename"),
                                mimeType = obj.getString("mime_type"),
                                sizeBytes = obj.getLong("size_bytes"),
                                category = obj.getString("category"),
                                includedInContext = obj.getBoolean("included_in_context"),
                                createdAt = dateFormat.parse(obj.getString("created_at").split(".")[0]) ?: Date(),
                                updatedAt = dateFormat.parse(obj.getString("updated_at").split(".")[0]) ?: Date(),
                                downloadUrl = obj.optString("download_url", null),
                            )
                        )
                    }

                    runOnUiThread {
                        if (allFiles.isEmpty()) {
                            Toast.makeText(this, "No files found", Toast.LENGTH_SHORT).show()
                        } else {
                            showFileSelectionDialog(allFiles)
                        }
                    }
                } else {
                    Log.e("ChatActivity", "Failed to load all files: ${response.code}")
                    runOnUiThread {
                        Toast.makeText(this, "Failed to load files", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error loading all files", e)
                runOnUiThread {
                    Toast.makeText(this, "Error loading files", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun showFileSelectionDialog(allFiles: List<ChatFile>) {
        val currentConvId = conversationId

        // Group files by conversation
        val filesByConv = allFiles.groupBy { it.conversationId }

        // Build dialog with checkboxes
        val dialogView = layoutInflater.inflate(android.R.layout.select_dialog_multichoice, null)
        val items = mutableListOf<String>()
        val fileList = mutableListOf<ChatFile>()

        filesByConv.forEach { (convId, files) ->
            val convLabel = if (convId == currentConvId) "This Chat" else "Chat: ${convId.take(8)}"
            files.forEach { file ->
                items.add("${file.filename}\n$convLabel")
                fileList.add(file)
            }
        }

        val checkedItems = fileList.map { it.includedInContext }.toBooleanArray()

        AlertDialog.Builder(this)
            .setTitle("Select Files to Include")
            .setMultiChoiceItems(items.toTypedArray(), checkedItems) { _, which, isChecked ->
                checkedItems[which] = isChecked
            }
            .setPositiveButton("Apply") { _, _ ->
                // Update included_in_context for changed files
                fileList.forEachIndexed { index, file ->
                    if (file.includedInContext != checkedItems[index]) {
                        updateFileContextCrossConversation(file, checkedItems[index])
                    }
                }
                // Refresh current file list
                loadChatFiles()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun updateFileContextCrossConversation(file: ChatFile, included: Boolean) {
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                val json = JSONObject().apply {
                    put("included_in_context", included)
                }

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/${file.conversationId}/files/${file.id}?allow_cross_conversation=true")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .addHeader("Content-Type", "application/json")
                    .patch(json.toString().toRequestBody("application/json".toMediaType()))
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (!response.isSuccessful) {
                    Log.e("ChatActivity", "Failed to update file context: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error updating file context", e)
            }
        }
    }

    private fun showGithubRepoDialog() {
        val convId = conversationId ?: run {
            Toast.makeText(this, "Please start a conversation first", Toast.LENGTH_SHORT).show()
            return
        }

        val input = EditText(this).apply {
            hint = "https://github.com/owner/repo"
            inputType = android.text.InputType.TYPE_TEXT_VARIATION_URI
        }

        AlertDialog.Builder(this)
            .setTitle("Add GitHub Repository")
            .setMessage("Enter the GitHub repository URL to include as reference:")
            .setView(input)
            .setPositiveButton(getString(R.string.action_ok)) { _, _ ->
                val repoUrl = input.text.toString().trim()
                if (repoUrl.isNotEmpty() && repoUrl.contains("github.com")) {
                    addGithubRepo(repoUrl)
                } else {
                    Toast.makeText(this, "Invalid GitHub URL", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton(getString(R.string.action_cancel), null)
            .show()
    }

    private fun addGithubRepo(repoUrl: String) {
        val convId = conversationId ?: return

        executor.execute {
            try {
                runOnUiThread {
                    Toast.makeText(this, "Adding GitHub repo...", Toast.LENGTH_SHORT).show()
                }

                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", BuildConfig.BACKEND_BASE_URL) ?: BuildConfig.BACKEND_BASE_URL

                val json = JSONObject().apply {
                    put("repo_url", repoUrl)
                    put("branch", "main")
                }

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/$convId/github/repos")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .addHeader("Content-Type", "application/json")
                    .post(json.toString().toRequestBody("application/json".toMediaType()))
                    .build()

                val response = BackendClient.executeWithRetry(request)

                if (response.isSuccessful) {
                    runOnUiThread {
                        Toast.makeText(this, "GitHub repo added", Toast.LENGTH_SHORT).show()
                        loadChatFiles()  // Reload to show the new repo
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(this, "Failed to add repo", Toast.LENGTH_SHORT).show()
                    }
                    Log.e("ChatActivity", "Add repo failed: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ChatActivity", "Error adding GitHub repo", e)
                runOnUiThread {
                    Toast.makeText(this, "Error adding repo", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // Load messages
    // -------------------------------------------------------------------------

    private fun loadMessages() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/api/chat")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                showError("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                showError("Error: HTTP ${resp.code}")
                            else -> {
                                val messages = runCatching {
                                    JSONObject(body).getJSONArray("messages")
                                }.getOrNull()
                                renderMessages(messages)
                            }
                        }
                    }
                }
            } catch (_: IOException) {
                // Best-effort: keep whatever is currently shown.
            }
        }
    }

    // -------------------------------------------------------------------------
    // Send message
    // -------------------------------------------------------------------------

    private fun onSendClicked() {
        val message = binding.etMessage.text.toString().trim()
        if (message.isBlank()) return

        binding.etMessage.setText("")
        binding.btnSend.isEnabled = false

        val agentMode = binding.switchAgentMode.isChecked
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        executor.execute {
            // GLOBAL_CONVERSATION_ACTIVATION_V1: lazy-init conversation on first send.
            if (conversationId == null) {
                Log.d("ChatActivity", "Creating new conversation...")
                conversationId = fetchNewConversationId(baseUrl, apiKey)
                if (conversationId != null) {
                    Log.d("ChatActivity", "Conversation created: ${conversationId}")
                }
            }

            val cid = conversationId
            if (cid == null) {
                Log.e("ChatActivity", "Conversation creation FAILED — aborting send")
                runOnUiThread {
                    showError("Error: conversation_id missing — initialization failure")
                    binding.btnSend.isEnabled = true
                }
                return@execute
            }

            val bodyJson = JSONObject().apply {
                put("message", message)
                put("conversation_id", cid)
                put("agent_mode", agentMode)
                put(
                    "context",
                    JSONObject().apply {
                        put("session_id", JSONObject.NULL)
                        put("domain_profile_id", JSONObject.NULL)

                        // Add file context
                        val includedFiles = chatFiles.filter { it.includedInContext }
                        if (includedFiles.isNotEmpty()) {
                            val filesArray = JSONArray()
                            includedFiles.forEach { file ->
                                filesArray.put(JSONObject().apply {
                                    put("id", file.id)
                                    put("filename", file.filename)
                                    put("category", file.category)
                                    put("mime_type", file.mimeType)
                                })
                            }
                            put("files", filesArray)
                        }
                    },
                )
            }.toString()

            val request = Request.Builder()
                .url("$baseUrl/api/chat")
                .post(bodyJson.toRequestBody("application/json".toMediaType()))
                .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                // Send X-Agent-Mode header as well so backends that read the header work.
                .addHeader("X-Agent-Mode", if (agentMode) "1" else "0")
                .build()

            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        showError(getString(R.string.status_chat_retrying, attempt, total))
                    }
                }
                response.use { resp ->
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                showError("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                showError("Error: HTTP ${resp.code}")
                            else -> loadMessages()
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    showError("Error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    // GLOBAL_CONVERSATION_ACTIVATION_V1: called from the worker thread before the first send.
    private fun fetchNewConversationId(baseUrl: String, apiKey: String): String? {
        val request = Request.Builder()
            .url("$baseUrl/api/chat/conversation/new")
            .post("{}".toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()
        return try {
            BackendClient.executeWithRetry(request).use { resp ->
                if (resp.isSuccessful) {
                    JSONObject(resp.body?.string() ?: "")
                        .optString("conversation_id")
                        .takeIf { it.isNotEmpty() }
                } else {
                    Log.e("ChatActivity", "Conversation creation FAILED — HTTP ${resp.code}")
                    null
                }
            }
        } catch (e: IOException) {
            Log.e("ChatActivity", "Conversation creation FAILED — ${e.message}")
            null
        }
    }

    // -------------------------------------------------------------------------
    // Edit message
    // -------------------------------------------------------------------------

    private fun showEditDialog(message: ChatMessageAdapter.Message) {
        val editText = EditText(this).apply {
            setText(message.content)
            setSelection(message.content.length)
        }

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_edit_message_title))
            .setView(editText)
            .setPositiveButton(getString(R.string.dialog_btn_save)) { _, _ ->
                val newContent = editText.text.toString().trim()
                if (newContent.isNotBlank()) {
                    submitEdit(message.id, newContent)
                }
            }
            .setNegativeButton(getString(R.string.dialog_btn_cancel), null)
            .show()
    }

    private fun submitEdit(messageId: String, newContent: String) {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val bodyJson = JSONObject().apply {
            put("content", newContent)
        }.toString()

        val request = Request.Builder()
            .url("$baseUrl/api/chat/$messageId/edit")
            .post(bodyJson.toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        binding.btnSend.isEnabled = false

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadMessages()
                        } else {
                            showError("Edit failed: HTTP ${resp.code}")
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    showError("Edit error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // Render
    // -------------------------------------------------------------------------

    private fun renderMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) {
            adapter.submitList(emptyList())
            return
        }

        val list = mutableListOf<ChatMessageAdapter.Message>()
        for (i in 0 until messages.length()) {
            val msg = messages.getJSONObject(i)
            list.add(
                ChatMessageAdapter.Message(
                    id = msg.optString("id"),
                    role = msg.optString("role"),
                    content = msg.optString("content"),
                    superseded = msg.optBoolean("superseded", false),
                )
            )

            // Extract conversation ID from messages for file loading
            if (conversationId == null) {
                val convId = msg.optString("conversation_id", null)
                if (convId != null && convId.isNotEmpty()) {
                    conversationId = convId
                }
            }
        }
        // API returns newest-first; reverse so the RecyclerView shows oldest-first
        // with stackFromEnd=true (most recent at bottom).
        list.reverse()
        adapter.submitList(list)
        binding.rvMessages.scrollToPosition(adapter.itemCount - 1)

        // Load files for current conversation
        conversationId?.let { loadChatFiles() }
    }

    // -------------------------------------------------------------------------
    // Multi-select toolbar
    // -------------------------------------------------------------------------

    private fun updateMultiSelectToolbar() {
        val inMultiSelect = adapter.isMultiSelectMode
        binding.toolbarMultiSelect.visibility = if (inMultiSelect) View.VISIBLE else View.GONE
        if (inMultiSelect) {
            val count = adapter.getSelectedMessages().size
            binding.tvSelectionCount.text = resources.getQuantityString(
                R.plurals.multi_select_count, count, count
            )
        }
    }

    // -------------------------------------------------------------------------
    // Clipboard / Share helpers
    // -------------------------------------------------------------------------

    private fun copyToClipboard(text: String) {
        val clipboard = ContextCompat.getSystemService(this, ClipboardManager::class.java)
        clipboard?.setPrimaryClip(ClipData.newPlainText("chat_message", text))
    }

    private fun shareText(text: String) {
        startActivity(
            Intent.createChooser(
                Intent(Intent.ACTION_SEND).apply {
                    type = "text/plain"
                    putExtra(Intent.EXTRA_TEXT, text)
                },
                getString(R.string.share_via)
            )
        )
    }

    private fun showError(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    // -------------------------------------------------------------------------
    // Voice / microphone input
    // -------------------------------------------------------------------------

    private fun setupMicButton() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            binding.btnMic.isEnabled = false
            return
        }
        binding.btnMic.setOnClickListener { onMicClicked() }
    }

    private fun onMicClicked() {
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startSpeechRecognition()
        } else {
            micPermissionLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun startSpeechRecognition() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.btn_mic))
        }
        speechInputLauncher.launch(intent)
    }
}
