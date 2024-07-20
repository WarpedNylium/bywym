var sidebar = document.getElementById("mySidebar");
var button_click_me = document.getElementById("click_me");

button_click_me.onclick = function() {
    // Toggle the 'hidden' class
    sidebar.classList.toggle('hidden');
}

var modal = document.getElementById("myModal");
var buttons_summary = document.getElementsByClassName("summary");
var close = document.getElementById("close");

for (var i = 0; i < buttons_summary.length; i++) {
    buttons_summary[i].onclick = async function() {
        // Get the URL from the button's data-url attribute
        var url = this.getAttribute('data-url');
        console.log("URL: ", url);

        // Update the modal content to show the processing status
        var modalContent = document.querySelector('.modal-content p');
        modalContent.innerHTML = 'Analyzing article, it may take a few seconds...';
        modalContent.className = 'fade-text';
        modal.style.display = "block";

        // Send a POST request to the /summarize route with the URL
        try {
            var response = await fetch('/summarize', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: 'url=' + encodeURIComponent(url)
            });
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            var data = await response.json();
            modalContent.textContent = data.summary ;
            modalContent.classList.remove('fade-text');  // Remove the animation class

            var note = document.createElement('p');
            note.innerHTML = 'Note: The contents might be distorted due to issues that lurk in the AI model.';
            note.style.fontSize = 'small';
            note.style.marginTop = '20px';
            modalContent.appendChild(note);

        } catch (error) {
            console.log('Fetch Error: ', error);
            modalContent.innerHTML = 'Oops, something went wrong : (<br>For New York Times and Economist :  you need to pay for the content.<br> For POLITICO, its policy do not allow web scarpe. <br> **Since the HTML structure varies, some unexpected errors may happen.**';
            modalContent.classList.remove('fade-text');  // Remove the animation class
        }
    }   
}

close.onclick = function() {
  modal.style.display = "none";
}

window.onclick = function(event) {
  if (event.target == modal) {
    modal.style.display = "none";
  }
}

// Function to fetch and display comments
function fetchComments(articleId) {
    $.ajax({
        url: '/articles/' + articleId + '/comments',
        method: 'GET',
        success: function(comments) {
            // Clear any existing comments
            $('#comments').empty();
            // Add each comment to the comments section
            comments.forEach(function(comment) {
                // Create a new div for each comment
                var commentDiv = $('<div class="comment"></div>');
                // Add the comment text to the div
                commentDiv.append('<p>' + comment + '</p>');  // Changed 'comment.content' to 'comment'
                // Add the div to the comments section
                $('#comments').append(commentDiv);
            });
        },
        error: function() {
            alert('Failed to fetch comments');
        }
    });
}

// When a "Discussion" button is clicked
$(document).on('click', '.discussion', function() {
    // Get the article ID from the data attribute on the button
    var articleId = $(this).data('article-id');
    // Set the article ID in the comment form
    $('#article_id').val(articleId);
    // Show the comment modal
    $('#commentModal').show();
    // Fetch and display comments
    fetchComments(articleId);
});

// When the user clicks on <span> (x), close the comment modal
$('#closeCommentModal').click(function() {
    $('#commentModal').hide();
});

// Function to show a temporary notification
function showNotification(message) {
    // Create a new div for the notification
    var notification = $('<div class="notification"></div>');
    // Add the message to the notification
    notification.text(message);
    // Add the notification to the body
    $('body').append(notification);
    // Remove the notification after 3 seconds
    setTimeout(function() {
        notification.remove();
    }, 3500);
}

// When the comment form is submitted
$('#comment-form').submit(function(e) {
    e.preventDefault();
    var articleId = $('#article_id').val();
    var content = $('#comment_content').val();
    $.ajax({
        url: '/comments',
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({ content: content, article_id: articleId }),
        success: function() {
            // Show a temporary notification
            showNotification('Comment posted successfully !');
            // Clear the comment form
            $('#comment_content').val('');
            // Fetch and display comments
            fetchComments(articleId);
        },
        error: function() {
            // Show a temporary notification
            showNotification('Failed to post comment');
        }
    });
});

// Loading spinner
var navLinks = document.querySelectorAll('nav a');
var sidebarLinks = document.querySelectorAll('#mySidebar a'); // Select all links in the sidebar

var linkGroups = [navLinks, sidebarLinks]; // Array of both groups of links

// Hide the loading spinner when the page finishes loading
window.addEventListener('load', function() {
    var loadingWrapper = document.getElementById('loading-wrapper');
    if (loadingWrapper) {
        loadingWrapper.style.display = 'none';
    }
});

linkGroups.forEach(function(links) { // Loop over each group of links
    for (var i = 0; i < links.length; i++) {
        links[i].addEventListener('click', function(e) {
            // Prevent the default action of the link
            e.preventDefault();

            // Get the loading wrapper
            var loadingWrapper = document.getElementById('loading-wrapper');

            // Show the loading spinner
            if (loadingWrapper) {
                loadingWrapper.style.display = 'block';
            }

            // Navigate to the new page
            window.location.href = e.target.href;
        });
    }
});