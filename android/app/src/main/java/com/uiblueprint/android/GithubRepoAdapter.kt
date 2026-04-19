package com.uiblueprint.android

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.RecyclerView
import com.uiblueprint.android.databinding.ItemGithubRepoBinding

data class GithubRepo(
    val name: String,
    val fullName: String,
    val description: String,
    val htmlUrl: String,
    val defaultBranch: String,
    val language: String,
    val stars: Int,
    val isPrivate: Boolean,
    var selected: Boolean = false,
    // Ingestion status from IngestJob (queued / running / success / failed)
    val ingestionStatus: String = "",
    val progress: Int = 0,  // 0-100
    val totalFiles: Int = 0,
    val totalChunks: Int = 0,
    val errorMessage: String? = null,  // Set when status == failed
    // IngestJob ID for polling
    val ingestJobId: String? = null,
)

/**
 * IngestJob response from GET /v1/ingest/{job_id}.
 * Source of truth for ingestion progress and status.
 */
data class IngestJobResponse(
    val job_id: String,
    val kind: String,  // "file" | "url" | "repo"
    val source: String,
    val status: String,  // "queued" | "running" | "success" | "failed"
    val progress: Int,  // 0-100
    val file_count: Int,
    val chunk_count: Int,
    val error: String?,
    val conversation_id: String?,
    val workspace_id: String?,
    val created_at: String,
    val updated_at: String
)

class GithubRepoAdapter(
    private val onSelectionChanged: ((GithubRepo) -> Unit)? = null,
) : RecyclerView.Adapter<GithubRepoAdapter.RepoViewHolder>() {

    private val repos = mutableListOf<GithubRepo>()
    
    companion object {
        private const val MAX_ERROR_MESSAGE_LENGTH = 100
    }

    fun submitList(newRepos: List<GithubRepo>) {
        repos.clear()
        repos.addAll(newRepos)
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RepoViewHolder {
        val binding = ItemGithubRepoBinding.inflate(
            LayoutInflater.from(parent.context),
            parent,
            false
        )
        return RepoViewHolder(binding)
    }

    override fun onBindViewHolder(holder: RepoViewHolder, position: Int) {
        holder.bind(repos[position])
    }

    override fun getItemCount(): Int = repos.size

    inner class RepoViewHolder(
        private val binding: ItemGithubRepoBinding
    ) : RecyclerView.ViewHolder(binding.root) {

        fun bind(repo: GithubRepo) {
            binding.tvRepoName.text = repo.fullName
            binding.tvRepoDescription.text = repo.description.ifEmpty { "No description" }
            binding.tvRepoLanguage.text = repo.language.ifEmpty { "Unknown" }
            binding.tvRepoStars.text = "⭐ ${repo.stars}"

            // UI STATE MAPPING (MANDATORY) — map backend status directly to UI display
            if (repo.ingestionStatus.isNotEmpty()) {
                val statusText = when (repo.ingestionStatus) {
                    "queued"  -> "Queued"
                    "running" -> "Processing"
                    "success" -> "Completed"
                    "failed"  -> "Failed"
                    else      -> repo.ingestionStatus
                }
                
                val statusEmoji = when (repo.ingestionStatus) {
                    "success" -> "✅"
                    "failed"  -> "❌"
                    "running" -> "⏳"
                    "queued"  -> "🕐"
                    else      -> "?"
                }
                
                // Build status label with progress and counts
                val statusLabel = when (repo.ingestionStatus) {
                    "success" -> "${statusEmoji} $statusText — ${repo.totalFiles} files, ${repo.totalChunks} chunks"
                    "failed"  -> {
                        // FAILURE VISIBILITY (CRITICAL) — display error message
                        val errorMsg = repo.errorMessage?.take(MAX_ERROR_MESSAGE_LENGTH) ?: "Unknown error"
                        "${statusEmoji} $statusText: $errorMsg"
                    }
                    "running" -> {
                        // Show live progress (0-100)
                        "${statusEmoji} $statusText (${repo.progress}%) — ${repo.totalFiles} files, ${repo.totalChunks} chunks"
                    }
                    "queued"  -> "${statusEmoji} $statusText"
                    else      -> "${statusEmoji} $statusText"
                }
                
                binding.tvRepoDescription.text =
                    "${binding.tvRepoDescription.text}  |  $statusLabel"
            }

            // Remove listener before setting checked state to avoid triggering callback
            binding.cbSelected.setOnCheckedChangeListener(null)
            binding.cbSelected.isChecked = repo.selected

            // Handle checkbox clicks
            binding.cbSelected.setOnCheckedChangeListener { _, isChecked ->
                repo.selected = isChecked
                onSelectionChanged?.invoke(repo)
            }

            // Handle item clicks (toggle checkbox)
            binding.root.setOnClickListener {
                binding.cbSelected.isChecked = !binding.cbSelected.isChecked
            }
        }
    }
}
