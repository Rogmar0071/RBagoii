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
    var selected: Boolean = false
)

class GithubRepoAdapter : RecyclerView.Adapter<GithubRepoAdapter.RepoViewHolder>() {

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
            
            // Remove listener before setting checked state to avoid triggering callback
            binding.cbSelected.setOnCheckedChangeListener(null)
            binding.cbSelected.isChecked = repo.selected

            // Handle checkbox clicks
            binding.cbSelected.setOnCheckedChangeListener { _, isChecked ->
                repo.selected = isChecked
            }

            // Handle item clicks (toggle checkbox)
            binding.root.setOnClickListener {
                binding.cbSelected.isChecked = !binding.cbSelected.isChecked
            }
        }
    }
}
