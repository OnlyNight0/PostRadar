# Category setup for existing PostRadar data

Startup creates the `categories` table and adds nullable `category_id` columns
to existing `sources` and `source_posts` tables. Existing rows and legacy
`Source.destination_channel_id` values are preserved. No Category is inferred
from an old channel title or destination.

To assign existing Sources after updating PostRadar:

1. Start the application and open the PostRadar bot chat.
2. Send `/start`, then `/categories`.
3. Add one Category for each destination, open its details, and set its
   destination with the channel `@username` or numeric ID. The bot must be an
   administrator with permission to post in that destination channel.
4. Send `/sources`, open each existing Source, choose **Change Category**, and
   select its default Category.

Until a legacy Source is assigned, its existing `destination_channel_id`
continues to be used for posts whose `SourcePost.category_id` is empty. New
Sources created in the bot require an enabled Category. A post keeps the
Category snapshot captured when it arrived, even if its Source is later moved.
An admin may override the Category for an individual post from its review
controls.

Removing a Source in the bot disables it and keeps its historical posts.
Categories referenced by Sources or historical posts cannot be deleted; they
can be disabled instead.
