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
    // REPO_CONTEXT_FINALIZATION_V1 — Phase 7: backend ingestion metadata
    val ingestionStatus: String = "",   // pending / running / success / failed
    val totalFiles: Int = 0,
    val totalChunks: Int = 0,
    // Set when this repo has been committed to the backend (has a server-side ID)
    val backendId: String? = null,
)

/**
 * REPO_CONTEXT_FINALIZATION_V1 — Phase 1.
 * First-class Repo entity returned by POST/GET /api/chat/{cid}/repos.
 */
data class RepoStatus(
    val id: String,
    val conversationId: String,
    val repoUrl: String,
    val owner: String,
    val name: String,
    val branch: String,
    val status: String,  // pending / running / success / failed
    val totalFiles: Int,
    val chunkCount: Int,
)

class GithubRepoAdapter(
    private val onSelectionChanged: ((GithubRepo) -> Unit)? = null,
) : RecyclerView.Adapter<GithubRepoAdapter.RepoViewHolder>() {

    private val repos = mutableListOf<GithubRepo>()

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

            // REPO_CONTEXT_FINALIZATION_V1 — Phase 7:
            // Show ingestion status and file count when backend metadata is available.
            if (repo.ingestionStatus.isNotEmpty()) {
                val statusEmoji = when (repo.ingestionStatus) {
                    "success" -> "✅"
                    "failed"  -> "❌"
                    "running" -> "⏳"
                    "pending" -> "🕐"
                    else      -> "?"
                }
                val statusLabel = when (repo.ingestionStatus) {
                    "success" -> "${statusEmoji} Ready — ${repo.totalFiles} files, ${repo.totalChunks} chunks"
                    "failed"  -> "${statusEmoji} Ingestion failed"
                    "running" -> "${statusEmoji} Ingesting…"
                    "pending" -> "${statusEmoji} Pending ingestion"
                    else      -> "${statusEmoji} ${repo.ingestionStatus}"
                }
                // Reuse tvRepoLanguage-adjacent space if available; fallback to description
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
