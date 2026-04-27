---
version: alpha
name: Airport Departures
description: Departure board: flap black, amber status, gate mono.
colors:
  primary: "#F2B53A"
  secondary: "#8E6F25"
  tertiary: "#F2F2EF"
  neutral: "#0D0D0D"
  surface: "#141413"
  on-primary: "#0D0D0D"
typography:
  display:
    fontFamily: IBM Plex Mono
    fontSize: 3rem
    fontWeight: 700
  h1:
    fontFamily: IBM Plex Mono
    fontSize: 1.7rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.1em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A split-flap departure-board palette: flap-black panel, amber status chars, disciplined mono.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F2B53A`):** Headlines and core text.
- **Secondary (`#8E6F25`):** Borders, captions, and metadata.
- **Tertiary (`#F2F2EF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0D0D0D`):** The page foundation.

## Typography

- **display:** IBM Plex Mono 3rem
- **h1:** IBM Plex Mono 1.7rem
- **body:** IBM Plex Mono 0.92rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
