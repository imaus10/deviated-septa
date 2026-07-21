I love public transit. In Philly, we're #blessed with an extensive system that includes subways, buses, AND (my fav) trolleys.

Even so, I've noticed that sometimes it seems less than reliable. But is it just my impatient perception? Are there certain routes that are worse than others? Perhaps certain parts of the city that are systemically unreliable? What do the data say about it?

So I started working on a system to track each route's deviation from its schedule. It's called...__Deviated SEPTA__ 😆

![screenshot of UI](./screenshot%20v0.1.png)

It polls the SEPTA real-time APIs every minute to measure a vehicle arrival at a given stop and aggregates over time to approximate route reliability.

The project has only just begun, so no firm conclusions yet. Let's see where it takes us!
