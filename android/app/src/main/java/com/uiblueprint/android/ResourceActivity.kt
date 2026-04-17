package com.uiblueprint.android

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.util.Log
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.uiblueprint.android.databinding.ActivityResourceBinding
import okhttp3.Request
import org.json.JSONArray
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * Resource screen for managing GitHub repositories and files available to AI.
 *
 * Features:
 * - Load GitHub repositories for a user
 * - Select repositories to include in AI context
 * - View and select files grouped by type
 * - Apply selections and return to chat
 */
class ResourceActivity : AppCompatActivity() {

    private lateinit var binding: ActivityResourceBinding
    private lateinit var prefs: SharedPreferences
    private val executor = Executors.newSingleThreadExecutor { Thread(it, "ResourceActivity-worker") }

    private lateinit var repoAdapter: GithubRepoAdapter
    private lateinit var fileAdapter: ChatFileAdapter

    private val githubRepos = mutableListOf<GithubRepo>()
    private val chatFiles = mutableListOf<ChatFile>()

    private var conversationId: String? = null

    companion object {
        private const val PREFS_NAME = "chat_prefs"
        const val EXTRA_CONVERSATION_ID = "conversation_id"

        fun start(context: Context, conversationId: String?) {
            val intent = Intent(context, ResourceActivity::class.java)
            intent.putExtra(EXTRA_CONVERSATION_ID, conversationId)
            context.startActivity(intent)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityResourceBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        conversationId = intent.getStringExtra(EXTRA_CONVERSATION_ID)

        setupRepoList()
        setupFileList()

        binding.btnClose.setOnClickListener {
            finish()
        }

        binding.btnLoadRepos.setOnClickListener {
            val username = binding.etGithubUsername.text.toString().trim()
            if (username.isNotEmpty()) {
                loadGithubRepos(username)
            } else {
                Toast.makeText(this, "Enter a GitHub username", Toast.LENGTH_SHORT).show()
            }
        }

        binding.btnApply.setOnClickListener {
            applySelections()
        }

        // Load files for the current conversation
        loadChatFiles()
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
    }

    private fun setupRepoList() {
        repoAdapter = GithubRepoAdapter()
        binding.rvGithubRepos.layoutManager = LinearLayoutManager(this)
        binding.rvGithubRepos.adapter = repoAdapter
    }

    private fun setupFileList() {
        fileAdapter = ChatFileAdapter(object : ChatFileAdapter.FileActionListener {
            override fun onFileClick(file: ChatFile) {
                // Toggle selection
                file.includedInContext = !file.includedInContext
                fileAdapter.notifyDataSetChanged()
            }

            override fun onFileOptionsClick(file: ChatFile) {
                // Not used in resource view
            }
        })
        binding.rvFiles.layoutManager = LinearLayoutManager(this)
        binding.rvFiles.adapter = fileAdapter
    }

    private fun loadGithubRepos(username: String) {
        executor.execute {
            try {
                runOnUiThread {
                    binding.tvNoRepos.visibility = View.GONE
                    Toast.makeText(this, "Loading repositories…", Toast.LENGTH_SHORT).show()
                }

                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

                val request = Request.Builder()
                    .url("$baseUrl/api/github/user/$username/repos?per_page=30")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .get()
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "[]"
                    val reposArray = JSONArray(body)
                    val repos = mutableListOf<GithubRepo>()

                    for (i in 0 until reposArray.length()) {
                        val obj = reposArray.getJSONObject(i)
                        repos.add(
                            GithubRepo(
                                name = obj.getString("name"),
                                fullName = obj.getString("full_name"),
                                description = obj.optString("description", ""),
                                htmlUrl = obj.getString("html_url"),
                                defaultBranch = obj.optString("default_branch", "main"),
                                language = obj.optString("language", ""),
                                stars = obj.optInt("stargazers_count", 0),
                                isPrivate = obj.optBoolean("private", false),
                                selected = false
                            )
                        )
                    }

                    runOnUiThread {
                        githubRepos.clear()
                        githubRepos.addAll(repos)
                        repoAdapter.submitList(githubRepos)
                        binding.tvNoRepos.visibility = if (repos.isEmpty()) View.VISIBLE else View.GONE
                        Toast.makeText(this, "Loaded ${repos.size} repositories", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(
                            this,
                            getString(R.string.error_github_load_failed),
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                    Log.e("ResourceActivity", "Failed to load repos: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error loading repos", e)
                runOnUiThread {
                    Toast.makeText(
                        this,
                        getString(R.string.error_github_load_failed),
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
        }
    }

    private fun loadChatFiles() {
        val convId = conversationId ?: return
        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

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
                        binding.tvNoFiles.visibility = if (files.isEmpty()) View.VISIBLE else View.GONE
                    }
                } else {
                    Log.e("ResourceActivity", "Failed to load files: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error loading files", e)
            }
        }
    }

    private fun applySelections() {
        val convId = conversationId ?: run {
            finish()
            return
        }

        executor.execute {
            try {
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

                // Add selected GitHub repos
                for (repo in githubRepos.filter { it.selected }) {
                    // Add repo to conversation
                    val jsonBody = JSONObject().apply {
                        put("repo_url", repo.htmlUrl)
                        put("branch", repo.defaultBranch)
                    }.toString()

                    val request = Request.Builder()
                        .url("$baseUrl/api/chat/$convId/github/repos")
                        .addHeader("Authorization", "Bearer $apiKey")
                        .addHeader("Content-Type", "application/json")
                        .post(okhttp3.RequestBody.Companion.toRequestBody(
                            jsonBody,
                            okhttp3.MediaType.Companion.toMediaType("application/json")
                        ))
                        .build()

                    BackendClient.executeWithRetry(request)
                }

                // Update file context inclusion
                for (file in chatFiles) {
                    val jsonBody = JSONObject().apply {
                        put("included_in_context", file.includedInContext)
                    }.toString()

                    val request = Request.Builder()
                        .url("$baseUrl/api/chat/$convId/files/${file.id}")
                        .addHeader("Authorization", "Bearer $apiKey")
                        .addHeader("Content-Type", "application/json")
                        .patch(okhttp3.RequestBody.Companion.toRequestBody(
                            jsonBody,
                            okhttp3.MediaType.Companion.toMediaType("application/json")
                        ))
                        .build()

                    BackendClient.executeWithRetry(request)
                }

                runOnUiThread {
                    Toast.makeText(this, "Selections applied", Toast.LENGTH_SHORT).show()
                    finish()
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error applying selections", e)
                runOnUiThread {
                    Toast.makeText(this, "Failed to apply selections", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }
}
