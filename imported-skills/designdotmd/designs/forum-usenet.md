---
version: alpha
name: Forum Usenet
description: BBS nostalgia: teletype ink, PhpBB gridlines.
colors:
  primary: "#131313"
  secondary: "#5E5E5E"
  tertiary: "#1A6DAF"
  neutral: "#E9E6DD"
  surface: "#F4F1E8"
  on-primary: "#F4F1E8"
typography:
  display:
    fontFamily: VT323
    fontSize: 3.25rem
    fontWeight: 400
  h1:
    fontFamily: IBM Plex Mono
    fontSize: 1.6rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.06em"
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

A classic-forum aesthetic: mono body, gridlined tables, one single accent for threads.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#131313`):** Headlines and core text.
- **Secondary (`#5E5E5E`):** Borders, captions, and metadata.
- **Tertiary (`#1A6DAF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E9E6DD`):** The page foundation.

## Typography

- **display:** VT323 3.25rem
- **h1:** IBM Plex Mono 1.6rem
- **body:** IBM Plex Mono 0.92rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
